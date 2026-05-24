"""me 路由：当前账号信息 / child profile / 会话管理。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.auth.deps import get_current_account, require_child
from app.auth.redis_client import get_redis
from app.chat.compression import CONTEXT_COMPRESS_THRESHOLD_TOKENS
from app.chat.context import build_context
from app.chat.graph import enqueue_audit, main_graph
from app.chat.locks import (
    acquire_session_lock,
    acquire_throttle_lock,
    release_session_lock,
    running_streams,
)
from app.chat.prompts import build_system_prompt
from app.chat.session_policy import (
    SHANGHAI,
    logical_day,
    should_switch_session,
    today_session_title,
)
from app.chat.sse import stream_graph_to_sse
from app.db import get_db
from app.models.accounts import ChildProfile, User
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from app.models.enums import MessageRole, MessageStatus, SessionStatus
from app.schemas.accounts import AccountOut, CurrentAccount
from app.schemas.children import ChildProfileOut
from app.schemas.sessions import (
    ChatStreamRequest,
    MessageListItem,
    MessageListResponse,
    SessionListItem,
    SessionListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/me", tags=["me"])


@router.get("", response_model=AccountOut)
async def get_me(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountOut:
    """返回当前登录账号的 AccountOut（供续期触发测试用）。"""
    user = await db.get(User, current.id)
    if user is None:
        raise HTTPException(404, "user not found")
    return AccountOut(
        id=user.id,
        role=user.role,
        family_id=user.family_id,
        phone=user.phone,
        is_active=user.is_active,
    )


@router.get("/profile", response_model=ChildProfileOut)
async def get_my_profile(
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChildProfileOut:
    """子账号查询自身 ChildProfile；parent token → 403；profile 不存在 → 404。"""
    profile = (
        await db.execute(select(ChildProfile).where(ChildProfile.child_user_id == current.id))
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(404, "profile not found")
    assert profile.gender is not None
    assert profile.birth_date is not None
    return ChildProfileOut(
        id=profile.child_user_id,
        nickname=profile.nickname,
        gender=profile.gender.value,
        birth_date=profile.birth_date,
    )


# ---------------------------------------------------------------------------
# cursor helpers (keyset pagination)
# ---------------------------------------------------------------------------


class InvalidCursor(HTTPException):
    """Raised when cursor decoding fails: bad base64, bad ISO, or bad UUID."""

    def __init__(self) -> None:
        super().__init__(400, "InvalidCursor")


def _encode_cursor(sort_key: datetime, row_id: str) -> str:
    if sort_key.tzinfo is not None:
        sort_key = sort_key.replace(tzinfo=None)
    return base64.urlsafe_b64encode(f"{sort_key.isoformat()}|{row_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode cursor into (sort_key as naive datetime, row_id as str). Raises InvalidCursor."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except Exception:
        raise InvalidCursor()

    parts = raw.rsplit("|", 1)
    if len(parts) != 2:
        raise InvalidCursor()
    sort_key_str, row_id = parts

    sort_key_dt: datetime | None = None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(sort_key_str, fmt)
            sort_key_dt = dt.replace(tzinfo=None)
            break
        except ValueError:
            continue
    _naive_fmts = (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    )
    for fmt in _naive_fmts:
        try:
            sort_key_dt = datetime.strptime(sort_key_str, fmt)
            break
        except ValueError:
            continue
    if sort_key_dt is None:
        raise InvalidCursor()

    try:
        UUID(row_id)
    except Exception:
        raise InvalidCursor()

    return sort_key_dt, row_id


# ---------------------------------------------------------------------------
# GET /me/sessions
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 15,
    cursor: str | None = None,
) -> SessionListResponse:
    """List sessions for the authenticated child (keyset pagination, no in_progress).

    M6-patch3：响应顶层附 today_session_id；sessions 数组过滤今日 logical_day。
    """
    if cursor is not None and cursor == "":
        cursor = None

    now = datetime.now(SHANGHAI)
    latest = (
        await db.execute(
            select(SessionModel)
            .where(SessionModel.child_user_id == current.id, SessionModel.status == "active")
            .order_by(SessionModel.last_active_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    today_sid = (
        latest.id if latest and logical_day(latest.last_active_at) == logical_day(now) else None
    )

    stmt = (
        select(SessionModel.id, SessionModel.title, SessionModel.last_active_at)
        .where(
            SessionModel.child_user_id == current.id,
            SessionModel.status == "active",
        )
        .order_by(SessionModel.last_active_at.desc(), SessionModel.id.desc())
        .limit(limit + 1)
    )
    if today_sid is not None:
        stmt = stmt.where(SessionModel.id != today_sid)
    if cursor:
        last_active_at_dt, sid = _decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(SessionModel.last_active_at, SessionModel.id) < (last_active_at_dt, sid),
        )
    result = await db.execute(stmt)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.last_active_at, str(last.id))
    else:
        next_cursor = None

    return SessionListResponse(
        sessions=[
            SessionListItem(id=row.id, title=row.title, last_active_at=row.last_active_at)
            for row in items
        ],
        today_session_id=today_sid,
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# GET /me/sessions/{id}/messages
# ---------------------------------------------------------------------------


@router.get("/sessions/{sid}/messages", response_model=MessageListResponse)
async def get_messages(
    sid: str,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> MessageListResponse:
    """Fetch messages for a session (keyset pagination, top-level in_progress)."""
    session_row = await db.get(SessionModel, sid)
    if session_row is None or session_row.status != "active":
        raise HTTPException(404, "SessionNotFound")
    if session_row.child_user_id != current.id:
        raise HTTPException(403, "SessionForbidden")

    if cursor is not None and cursor == "":
        cursor = None

    stmt = (
        select(
            Message.id,
            Message.role,
            Message.content,
            Message.status,
            Message.finish_reason,
            Message.created_at,
        )
        .where(
            Message.session_id == sid,
            Message.role.in_([MessageRole.human, MessageRole.ai]),
            Message.status != MessageStatus.discarded,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        created_at_dt, mid = _decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(Message.created_at, Message.id) < (created_at_dt, mid),
        )
    result = await db.execute(stmt)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.created_at, str(last.id))
    else:
        next_cursor = None

    try:
        in_progress = bool(await redis.exists(f"chat:lock:{sid}"))
    except RedisError as e:
        in_progress = False
        logger.warning(
            "redis exists failed for chat:lock:%s, fallback in_progress=False: %s", sid, e
        )

    return MessageListResponse(
        items=[
            MessageListItem(
                id=row.id,
                role=row.role,
                content=row.content,
                status=row.status,
                finish_reason=row.finish_reason,
                created_at=row.created_at,
            )
            for row in items
        ],
        next_cursor=next_cursor,
        in_progress=in_progress,
    )


# ---------------------------------------------------------------------------
# DELETE /me/sessions/{id}
# ---------------------------------------------------------------------------


@router.delete("/sessions/{sid}", status_code=204)
async def delete_session(
    sid: str,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Soft-delete a session (status='deleted'). Idempotent: second call → 404."""
    session = (
        await db.execute(select(SessionModel).where(SessionModel.id == sid))
    ).scalar_one_or_none()

    if session is None or session.status == "deleted":
        raise HTTPException(404, "SessionNotFound")

    if session.child_user_id != current.id:
        raise HTTPException(403, "SessionForbidden")

    session.status = SessionStatus.deleted
    await db.commit()


# ---------------------------------------------------------------------------
# POST /me/sessions/{id}/stop
# ---------------------------------------------------------------------------


@router.post("/sessions/{sid}/stop", status_code=204)
async def stop_session(
    sid: str,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """停止正在运行的对话流（best-effort）。

    向 running_streams 中对应 sid 的 asyncio.Event 发送信号，使 generator 在
    下一次 yield 前退出。无论 event 是否存在都返回 204（best-effort 语义）。
    软删 session（status='deleted'）对客户端不可见，返回 404。
    """
    session = (
        await db.execute(
            select(SessionModel).where(
                SessionModel.id == sid,
                SessionModel.status == "active",
            )
        )
    ).scalar_one_or_none()

    if session is None:
        raise HTTPException(404, "SessionNotFound")

    if session.child_user_id != current.id:
        raise HTTPException(403, "SessionForbidden")

    event = running_streams.get(sid)
    if event is not None:
        event.set()
    # 始终返回 204（async best-effort，generator 后续在 finally 处理剩余清理）


# ---------------------------------------------------------------------------
# POST /me/chat/stream
# ---------------------------------------------------------------------------


def _frame_sse_event(event_type: str, data: dict) -> bytes:
    """SSE 多行协议帧（M6）：event: <type>\\ndata: <json>\\n\\n。"""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def _stub_stream() -> list[bytes]:
    """LLM 桩流：产生一个 delta 帧和一个 end 帧。"""
    return [
        _frame_sse_event("delta", {"content": "[stub]"}),
        _frame_sse_event("end", {"finish_reason": "stop", "aid": None}),
    ]


@router.post("/chat/stream")
async def chat_stream(
    req: ChatStreamRequest,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> StreamingResponse:
    """子对话轮次流式接口：决策矩阵 + 图谱流 + T5 持久化。

    流程：限流锁 → 会话检查 → 会话锁 → 决策矩阵
    → 首轮/后续轮事务 → 提交① → 图谱流
    （main_graph.astream custom-mode）→ stream_graph_to_sse → T5
    （persist_ai_turn 写入 ai active 行）→ 提交② → StreamingResponse。

    双提交边界：提交①（L597）持久化 human 行 + session；
    提交②（generator 内 L758/L792）持久化 AI 行
    （persist_ai_turn flush + db.commit）。两次提交 = 两个原子
    单元：错误路径不写 ai 行；图谱流失败不回滚 human 行。

    Decision matrix O (baseline §5.4, 7 rows):
      Row 1: last=None   + regen=null  → INSERT human (active) [session resolved via policy]
      Row 2: last=None   + regen=!null → 400 RegenerateForInvalid
      Row 3: last=AI      + regen=null  → INSERT human (active)
      Row 4: last=AI      + regen=!null → 400 RegenerateForInvalid
      Row 5: last=orphan  + regen=null  → UPDATE old discarded + INSERT human (active)
      Row 6: last=orphan  + regen=hid   → reuse orphan (content must be "")
      Row 7: last=orphan  + regen=!hid  → 400 RegenerateForInvalid

    Gate A 闭合论证（适用于 Row 5-7）：
      "末条 active 消息"定义为 SELECT ... WHERE status='active'
      ORDER BY created_at DESC, id DESC LIMIT 1 —— 它始终是最新的 active 行。
      "非孤儿 human"需要在它之后存在一条 active AI 行，
      但该 AI 行本身会成为"最新的 active 行"，与 ORDER BY 结果矛盾。
      因此"末条 active 行是 human" ⟺ "孤儿 human"；无需二次查询。
      Row 8/9（非孤儿 human 路径）不可达——若执行到则 raise AssertionError，
      以捕获未来状态空间回归。

    T5 单写入点（Step 8b）：图谱流结束 → persist_ai_turn
    写入恰好一条 ai active 行（status='active', role='ai'），
    内容为 accumulated 累积内容，finish_reason 取自末条 graph chunk；
    同时更新 sessions.last_active_at。错误路径不写 ai 行。

    SSE 7-event sequence (M6, §8.1):
      session_meta → [thinking_start → thinking_end] → delta×N → end
    session_meta.session_id 始终为服务端最终生效 sid（非客户端传入的 hint）。
    错误路径：发射 error 帧（human active 行保留，不写 ai 行）。

    锁释放契约：session 锁在 generator finally 块中释放
    （成功路径）；对于在 generator 构造前抛出的 HTTPException
    路径（P0-2），在 StreamingResponse 前显式释放。

    Stop 检测（Step 8c）：session_meta 产出后，注册
    running_streams[sid] = asyncio.Event()。每次 for 循环迭代在
    内容累积之后、SSE yield 之前检查 event.is_set()（关注点1）。
    StopKind 二分支：has_emitted_content ? StopWithAi（persist_ai_turn
    写入 'user_stopped' + 带 aid 的 stopped 帧） : StopNoAi（不带 aid 的 stopped 帧）。
    不 cancel：单帧 yield 用 try/except 包裹
    ConnectionError/anyio.BrokenResourceError/asyncio.CancelledError →
    client_alive=False，LLM 流继续但不 yield。
    """
    # ---- throttle lock (TTL 自然过期，finally 不主动 DEL) ----
    if not await acquire_throttle_lock(redis, str(current.id)):
        raise HTTPException(429, "RequestThrottled")

    # ---- session policy resolution（确定生效 sid） ----
    now = datetime.now(SHANGHAI)
    latest = (
        await db.execute(
            select(SessionModel)
            .where(SessionModel.child_user_id == current.id, SessionModel.status == "active")
            .order_by(SessionModel.last_active_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    sid: UUID
    session: SessionModel
    is_new_session: bool

    if should_switch_session(latest.last_active_at if latest else None, now):
        sid = uuid4()
        session = SessionModel(
            id=sid,
            child_user_id=current.id,
            title=today_session_title(now),
            status="active",
            last_active_at=now,
        )
        db.add(session)
        is_new_session = True
    else:
        sid = latest.id
        session = latest
        is_new_session = False

    # ---- session lock（新建 session 无 race，但为简化统一 lock） ----
    nonce = await acquire_session_lock(redis, str(sid))
    if not nonce:
        raise HTTPException(409, "SessionBusy")

    # 确保在 StreamingResponse 之前抛出 HTTPException 时释放锁
    try:
        # ---- session ownership check（仅复用 session 需验证） ----
        if not is_new_session and session.child_user_id != current.id:
            raise HTTPException(403, "SessionForbidden")

        # ---- 准备 child_profile 数据（无 DB 写入依赖） ----
        child_profile = await db.get(ChildProfile, current.id)
        if child_profile is not None:
            from app.chat.prompts import compute_age

            _age = compute_age(child_profile.birth_date)
            _gender = child_profile.gender.value if child_profile.gender else None
        else:
            _age = 8  # 兜底默认值
            _gender = None

        # ---- decision matrix O + first-turn / subsequent-turn transaction ----
        last_msg = (
            await db.execute(
                select(Message)
                .where(Message.session_id == sid, Message.status == MessageStatus.active)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        hid: UUID  # human 消息 ID，用于 session_meta 事件
        user_msg: Message | None = None  # 追踪本轮新增的 human message，供 commit① 用

        if last_msg is None:
            # Row 1 或 Row 2
            if req.regenerate_for is not None:
                raise HTTPException(400, "RegenerateForInvalid")
            # Row 1：首轮（INSERT human active；session 已在策略解析中建好）
            human = Message(
                session_id=sid,
                role=MessageRole.human,
                status=MessageStatus.active,
                content=req.content,
            )
            db.add(human)
            await db.flush()
            hid = human.id
            user_msg = human
        # Row 3：末条为 AI，regen=null → INSERT human
        elif last_msg.role == MessageRole.ai:
            if req.regenerate_for is not None:
                raise HTTPException(400, "RegenerateForInvalid")
            human = Message(
                session_id=sid,
                role=MessageRole.human,
                status=MessageStatus.active,
                content=req.content,
            )
            db.add(human)
            await db.flush()
            hid = human.id
            user_msg = human
        else:
            # last_msg.role == MessageRole.human
            # Gate A 闭合论证：last_msg 是按 ORDER BY created_at DESC, id DESC LIMIT 1
            # 查询得到的"最新 active 行"。"非孤儿 human"意味着有一条 active AI 行
            # 严格排在它之后——但该 AI 行本身会成为"最新 active 行"，与 ORDER BY 结果矛盾。
            # 因此"末条 active 行是 human" ⟺ "孤儿 human"（穷举，无需二次查询）。
            is_orphan = last_msg.role == MessageRole.human

            if is_orphan:
                # Row 5、6、7
                if req.regenerate_for is None:
                    # Row 5：孤儿 + null → UPDATE 旧行 discarded + INSERT 新行
                    await db.execute(
                        update(Message)
                        .where(Message.id == last_msg.id)
                        .values(status=MessageStatus.discarded),
                    )
                    new_human = Message(
                        session_id=sid,
                        role=MessageRole.human,
                        status=MessageStatus.active,
                        content=req.content,
                    )
                    db.add(new_human)
                    await db.flush()
                    hid = new_human.id
                    user_msg = new_human
                elif req.regenerate_for == str(last_msg.id):
                    # Row 6：孤儿 + =hid → 复用孤儿行（不新增行，不更新内容）
                    if req.content != "":
                        raise HTTPException(400, "RegenerateForInvalid")
                    hid = last_msg.id
                    # user_msg 保持 None — 复用已有消息，不新增
                else:
                    # Row 7：孤儿 + ≠hid → 400
                    raise HTTPException(400, "RegenerateForInvalid")
            else:
                # 非孤儿 human 不可能成为末条 active 行（Gate A 闭合论证）。
                # 此分支不可达——raise 以捕获未来状态空间错误。
                raise AssertionError("unreachable: non-orphan human cannot be last active row")

        # ---- 构建 history + system prompt（在 decision matrix + flush 后，orphan discard 已可见） ----
        history = await build_context(sid, db)
        system_prompt = build_system_prompt(_age, _gender)

        # commit① — user 消息落库（同事务内同步 last_active_at）
        if user_msg is not None:
            session.last_active_at = user_msg.created_at
        await db.commit()

        # ---- 流式响应 ----
        async def generator() -> AsyncGenerator[bytes, None]:
            accumulated = ""
            last_finish_reason = "stop"  # 兜底；末帧 finish_reason 命中时覆盖
            usage_meta: dict | None = None  # 由 call_main_llm 转发的末帧 usage 快照
            has_emitted_content = False
            client_alive = True
            user_stopped = False

            # 为主图谱构建初始状态

            from app.chat.state import MainDialogueState
            from app.config import settings as _app_settings

            # M8: ai_turn_counter → turn_number（下一轮号）
            _turn_number = (session.ai_turn_counter or 0) + 1

            initial_state: MainDialogueState = {
                "session_id": str(sid),
                "child_user_id": str(current.id),
                "child_profile": None,  # M6：节点不读取此字段
                "provider": _app_settings.main_provider,
                "messages": [system_prompt, *history],
                "audit_state": {},  # load_audit_state 节点填充
                "pending_guidance": None,
                "generated_token_count": 0,
                "client_alive": True,
                "user_stop_requested": False,
                "turn_number": _turn_number,
            }

            try:
                yield _frame_sse_event("session_meta", {"session_id": str(sid), "hid": str(hid)})

                # 注册 stop event（Step 8c）
                event = asyncio.Event()
                running_streams[str(sid)] = event

                # 消费图谱流：累积内容 + 转发到 SSE
                thinking_started = False  # thinking 信号状态机（基线 §3.2）
                # ---- 阻塞压缩检查（scheme R）：needs_compression=True → 同步压缩 ----
                if session.needs_compression:
                    try:
                        yield _frame_sse_event("compression_start", {})

                        from app.chat.compression import build_compression_prompt
                        from app.chat.context import _to_lc_message
                        from app.chat.factory import get_chat_llm as _get_compression_llm

                        # R+: 计算受保护行（本轮新 human / 复用 orphan）
                        protected_id = user_msg.id if user_msg is not None else last_msg.id

                        # R+: 查询待压缩集，排除受保护行
                        actives_orm = (
                            (
                                await db.execute(
                                    select(Message)
                                    .where(
                                        Message.session_id == sid,
                                        Message.status == "active",
                                        Message.id != protected_id,
                                    )
                                    .order_by(Message.created_at.asc())
                                )
                            )
                            .scalars()
                            .all()
                        )

                        if actives_orm:
                            # R+: 自组装 summarizer 输入，不调 build_context（避免新 human 和排序问题）
                            c_input = build_compression_prompt(
                                [_to_lc_message(mo) for mo in actives_orm]
                            )
                            c_llm = _get_compression_llm()
                            c_result = await c_llm.ainvoke(c_input)
                            for mo in actives_orm:
                                mo.status = "compressed"
                            db.add(
                                Message(
                                    session_id=sid,
                                    role=MessageRole.summary,
                                    status=MessageStatus.active,
                                    content=c_result.content
                                    if hasattr(c_result, "content")
                                    else str(c_result),
                                )
                            )
                        else:
                            logger.info(
                                "compression noop for session %s: no messages to compress", sid
                            )

                        session.needs_compression = False
                        await db.commit()

                        # R+: 手动构造 initial_state["messages"]（方案 a）
                        # 顺序：[main_system, (turn_summ?), summary, protected_human]
                        # 不调 build_context（ASC 排序会将 protected_human 置于 summary 前）
                        _sp = build_system_prompt(_age, _gender)
                        _new_hist = []

                        # 若有新摘要行，注入（位于 protected_human 之前）
                        if actives_orm:
                            _summary_msg = (
                                await db.execute(
                                    select(Message)
                                    .where(
                                        Message.session_id == sid,
                                        Message.role == MessageRole.summary,
                                        Message.status == MessageStatus.active,
                                    )
                                    .order_by(Message.created_at.desc())
                                    .limit(1)
                                )
                            ).scalar_one()
                            _new_hist.append(_to_lc_message(_summary_msg))

                        # 受保护 human 行放在最后
                        _protected_msg = (
                            await db.execute(select(Message).where(Message.id == protected_id))
                        ).scalar_one()
                        _new_hist.append(_to_lc_message(_protected_msg))

                        initial_state["messages"] = [_sp, *_new_hist]

                        yield _frame_sse_event("compression_end", {})
                    except Exception:
                        logger.exception("compression failed for session %s", sid)
                        yield _frame_sse_event(
                            "error",
                            {
                                "message": "压缩失败，请重试",
                                "code": "CompressionError",
                            },
                        )
                        return

                async for payload in main_graph.astream(initial_state, stream_mode="custom"):
                    # 关注点1（修订）：stop 检查移至 for 顶部 —— 每个 chunk iter 进来即检查，
                    # 不论 payload 类型（reasoning / delta / usage_metadata 等）。
                    # 修复两个旧位置（原 reasoning continue 之后、SSE yield 之前）的 bug：
                    #   B1. reasoning 分支 `continue` 跳过原检查 → 思考阶段 stop 要等首 content delta（5-30s）才生效
                    #   B2. 原检查在 accumulated += d 之后但 yield 之前 → StopWithAi persist 了客户端未收到的最后一帧
                    # 顶部检查保证 has_emitted_content / accumulated 仅反映「已成功 yield 的 chunks」，前后端一致。
                    if event.is_set():
                        user_stopped = True
                        break
                    # usage_metadata 快照（由 call_main_llm 末帧转发）
                    if payload.get("usage_metadata"):
                        usage_meta = payload["usage_metadata"]

                    # reasoning 信号（基线 §3.2, signal-only, 不传文本）
                    if payload.get("reasoning"):
                        if not thinking_started and client_alive:
                            thinking_started = True
                            try:
                                yield _frame_sse_event("thinking_start", {})
                            except (
                                ConnectionError,
                                anyio.BrokenResourceError,
                                asyncio.CancelledError,
                            ):
                                client_alive = False
                        continue  # reasoning 信号 payload 不进 _wrap → stream_graph_to_sse

                    d = payload.get("delta", "")
                    if d:
                        has_emitted_content = True
                        accumulated += d

                    # 首个非空 delta 到达且 thinking 已开 → 收 thinking
                    if d and thinking_started and client_alive:
                        thinking_started = False
                        try:
                            yield _frame_sse_event("thinking_end", {})
                        except ConnectionError, anyio.BrokenResourceError, asyncio.CancelledError:
                            client_alive = False

                    fr = payload.get("finish_reason")
                    if fr:
                        last_finish_reason = fr  # stop / length / content_filter

                    # 关注点1 已上移至 for 循环顶部（修订）—— 修复 B1 + B2 见上

                    # 关注点4：单帧 yield 级 try/except + stream_graph_to_sse 保护
                    async def _wrap():
                        yield payload

                    try:
                        async for frame in stream_graph_to_sse(_wrap()):
                            if not client_alive:
                                continue
                            try:
                                yield frame
                            except (
                                ConnectionError,
                                anyio.BrokenResourceError,
                                asyncio.CancelledError,
                            ):
                                client_alive = False
                    except ConnectionError, anyio.BrokenResourceError, asyncio.CancelledError:
                        client_alive = False

                # 关注点3：user_stopped 与自然结束互斥（persist_ai_turn 至多 1 次）
                # ---- commit② — ai 消息落库 + usage 快照 + needs_compression 标志 + 审查 ----
                if user_stopped:
                    # 关注点2：StopKind 二分支
                    if has_emitted_content:
                        ai_msg = Message(
                            session_id=sid,
                            role=MessageRole.ai,
                            content=accumulated,
                            status=MessageStatus.active,
                            finish_reason="user_stopped",
                        )
                        db.add(ai_msg)
                        await db.flush()
                        aid = ai_msg.id

                        # usage 快照：写 LLM 真值，不累加
                        if usage_meta:
                            _usage_total = usage_meta["input_tokens"] + usage_meta["output_tokens"]
                            session.context_size_tokens = _usage_total
                            if _usage_total >= CONTEXT_COMPRESS_THRESHOLD_TOKENS:
                                session.needs_compression = True
                        await db.commit()

                        await enqueue_audit(sid, db, _turn_number)

                        if client_alive:
                            yield _frame_sse_event(
                                "stopped",
                                {"finish_reason": "user_stopped", "aid": str(aid)},
                            )
                    else:
                        if client_alive:
                            yield _frame_sse_event(
                                "stopped",
                                {"finish_reason": "user_stopped"},
                            )
                else:
                    # 自然结束分支：commit②
                    ai_msg = Message(
                        session_id=sid,
                        role=MessageRole.ai,
                        content=accumulated,
                        status=MessageStatus.active,
                        finish_reason=last_finish_reason,
                    )
                    db.add(ai_msg)
                    await db.flush()
                    aid = ai_msg.id

                    # usage 快照：写 LLM 真值，不累加
                    if usage_meta:
                        _usage_total = usage_meta["input_tokens"] + usage_meta["output_tokens"]
                        session.context_size_tokens = _usage_total
                        if _usage_total >= CONTEXT_COMPRESS_THRESHOLD_TOKENS:
                            session.needs_compression = True
                    await db.commit()

                    await enqueue_audit(sid, db, _turn_number)

                    if client_alive:
                        yield _frame_sse_event(
                            "end",
                            {"finish_reason": last_finish_reason, "aid": str(aid)},
                        )
            except Exception as e:
                logger.exception("chat_stream generator failed")
                if client_alive:
                    yield _frame_sse_event(
                        "error",
                        {"message": str(e), "code": "InternalError"},
                    )
                # human active 行已由上方 db.commit() 持久化，不写 ai 行（不回滚）
            finally:
                # 关注点5：先 pop running_streams（去除 stop 入口），再 release_session_lock
                running_streams.pop(str(sid), None)
                await release_session_lock(redis, str(sid), nonce)

        return StreamingResponse(generator(), media_type="text/event-stream")

    except HTTPException:
        await release_session_lock(redis, str(sid), nonce)
        raise
