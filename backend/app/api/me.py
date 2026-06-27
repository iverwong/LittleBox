"""子端与父端共用的"我"路由:``/api/v1/me``。

子端高频接口(``/me/sessions``、``/me/chat/stream``、``/me/sessions/{sid}/*``) +
子端自身资料(``/me/profile``) + 当前账号信息(``/me``)。HTTP 协议层只做
路由编排与依赖注入,业务编排(LLM pipeline / 状态机)走 ``domain/chat/*``。"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.db import get_db
from app.core.enums import MessageRole, MessageStatus, SessionStatus
from app.core.locks import (
    CHAT_LOCK_KEY_PREFIX,
    acquire_session_lock,
    acquire_throttle_lock,
    release_session_lock,
)
from app.core.redis import get_redis
from app.core.runtime import RuntimeResources
from app.core.time import now_shanghai
from app.domain.accounts.models import ChildProfile, User
from app.domain.accounts.schemas import (
    AccountOut,
    ChildProfileOut,
    ChildProfileSnapshot,
    CurrentAccount,
)
from app.domain.accounts.service import (
    load_child_profile,
)
from app.domain.auth.deps import get_current_account, require_child
from app.domain.chat.context_schema import ChatContextSchema
from app.domain.chat.models import Message
from app.domain.chat.models import Session as SessionModel
from app.domain.chat.pagination import decode_cursor, encode_cursor
from app.domain.chat.pipeline import run_llm_pipeline
from app.domain.chat.schemas import (
    ChatStreamRequest,
    MessageListItem,
    MessageListResponse,
    SessionListItem,
    SessionListResponse,
)
from app.domain.chat.session_policy import (
    should_switch_session,
    today_session_title,
)
from app.domain.chat.stream import ChatStreamState, stream_generator
from app.domain.chat.stream_signals import running_streams
from app.domain.chat.turn_intake import intake_human_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/me", tags=["me"])


# ---------------------------------------------------------------------------
# 路由 (HTTP handlers)
# ---------------------------------------------------------------------------


@router.get("", response_model=AccountOut)
async def get_me(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountOut:
    """返回当前登录账号的 ``AccountOut``,``GET /api/v1/me``。

    仅复述数据库中账号字段,不读取 / 暴露 ``password_hash``。

    Args:
        current: 当前账号上下文(``Depends(get_current_account)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。

    Returns:
        ``AccountOut``:当前账号的对外视图。

    Raises:
        HTTPException ``status.HTTP_404_NOT_FOUND``:
            token 对应的 User 行已不存在(理论上不该发生)。
    """
    user = await db.get(User, current.id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
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
    """子账号查询自身 ``ChildProfile``,``GET /api/v1/me/profile``。仅 child。

    parent token 命中 → ``403``(``require_child`` 抛);profile 未建
    → ``404``。

    Args:
        current: 当前账号上下文(``Depends(require_child)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。

    Returns:
        ``ChildProfileOut``:子账号资料。

    Raises:
        HTTPException ``status.HTTP_401_UNAUTHORIZED``:缺失 / 非法 Bearer token。
        HTTPException ``status.HTTP_403_FORBIDDEN``:非 child 角色。
        HTTPException ``status.HTTP_404_NOT_FOUND``:
            ChildProfile 行不存在(账号未初始化完成)。
    """
    profile = (
        await db.execute(select(ChildProfile).where(ChildProfile.child_user_id == current.id))
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "profile not found")
    return ChildProfileOut(
        child_user_id=profile.child_user_id,
        nickname=profile.nickname,
        gender=profile.gender,
        birth_date=profile.birth_date,
    )


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 15,
    cursor: str | None = None,
) -> SessionListResponse:
    """列出当前 child 的活跃 session,``GET /api/v1/me/sessions``。仅 child。

    支持 keyset 分页(``cursor`` + ``limit``),响应顶层附
    ``today_session_id``。``today_session_id`` 计算与 ``/me/chat/stream``
    入口保持一致:用 ``should_switch_session`` 同时覆盖自然日 R1 与跨日
    R2/R3 三种切日规则,确保 list 暴露的 ``today_session_id`` 与
    chat/stream 实际新建的 session 不会冲突(否则客户端跳到旧 session
    反而触发新建)。

    Args:
        current: 当前账号上下文(``Depends(require_child)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。
        limit: 每页条数,``[1, 50]``。
        cursor: keyset 分页游标(``None`` 或空串视为首页)。

    Returns:
        ``SessionListResponse``:session 列表 + ``today_session_id`` +
        ``next_cursor``。``in_progress`` session 也会出现在列表里(本端点
        不过滤,客户端按需处理)。

    Raises:
        HTTPException ``status.HTTP_401_UNAUTHORIZED``:缺失 / 非法 Bearer token。
        HTTPException ``status.HTTP_403_FORBIDDEN``:非 child 角色。
    """
    if cursor is not None and cursor == "":
        cursor = None

    now = now_shanghai()
    latest = (
        await db.execute(
            select(SessionModel)
            .where(
                SessionModel.child_user_id == current.id,
                SessionModel.status == SessionStatus.active,
            )
            .order_by(SessionModel.last_active_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    # 与 chat/stream 入口保持一致:自然日 R1 不切 + 跨日 R2/R3 切(should_switch_session)。
    # R1 按 create_at 锚定自然日,R2/R3 按 last_active_at 判 gap 与 04:00 硬切,
    # 详情见 app.domain.chat.session_policy 模块 docstring。
    today_sid = (
        latest.id
        if latest and not should_switch_session(latest.last_active_at, latest.created_at, now)
        else None
    )

    stmt = (
        select(SessionModel.id, SessionModel.title, SessionModel.last_active_at)
        .where(
            SessionModel.child_user_id == current.id,
            SessionModel.status == SessionStatus.active,
        )
        .order_by(SessionModel.last_active_at.desc(), SessionModel.id.desc())
        .limit(limit + 1)
    )
    if today_sid is not None:
        stmt = stmt.where(SessionModel.id != today_sid)
    if cursor:
        last_active_at_dt, sid = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(SessionModel.last_active_at, SessionModel.id) < (last_active_at_dt, sid),
        )
    result = await db.execute(stmt)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(last.last_active_at, str(last.id))
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


@router.get("/sessions/{sid}/messages", response_model=MessageListResponse)
async def get_messages(
    sid: str,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> MessageListResponse:
    """列出某 session 的消息,``GET /api/v1/me/sessions/{sid}/messages``。仅 child。

    keyset 分页,过滤 ``role in (human, ai)`` 与 ``status != discarded``。
    响应顶层附 ``in_progress``(Redis ``chat:lock:{sid}`` key 是否存在),
    供前端判断是否展示"AI 正在回复"。

    Args:
        sid: session id(path param)。
        current: 当前账号上下文(``Depends(require_child)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。
        redis: 主业务 Redis(``Depends(get_redis)``)。
        limit: 每页条数,``[1, 100]``。
        cursor: keyset 分页游标(``None`` 或空串视为首页)。

    Returns:
        ``MessageListResponse``:消息列表 + ``next_cursor`` + ``in_progress``。

    Raises:
        HTTPException ``status.HTTP_404_NOT_FOUND``:session 不存在或已删除。
        HTTPException ``status.HTTP_403_FORBIDDEN``:session 不属于当前 child。
    """
    session_row = await db.get(SessionModel, sid)
    if session_row is None or session_row.status != SessionStatus.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SessionNotFound")
    if session_row.child_user_id != current.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "SessionForbidden")

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
        created_at_dt, mid = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(Message.created_at, Message.id) < (created_at_dt, mid),
        )
    result = await db.execute(stmt)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(last.created_at, str(last.id))
    else:
        next_cursor = None

    try:
        in_progress = bool(await redis.exists(f"{CHAT_LOCK_KEY_PREFIX}{sid}"))
    except RedisError as e:
        # Redis 不可用时降级:不阻塞列表查询,前端不显示"正在回复"提示
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


@router.delete("/sessions/{sid}", status_code=204)
async def delete_session(
    sid: str,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """软删 session(``status='deleted'``),``DELETE /api/v1/me/sessions/{sid}``。仅 child。

    幂等:已是 ``deleted`` 状态再调一次仍返回 ``404``,与"不存在"统一。

    Args:
        sid: session id(path param)。
        current: 当前账号上下文(``Depends(require_child)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。

    Returns:
        无。

    Raises:
        HTTPException ``status.HTTP_404_NOT_FOUND``:session 不存在或已软删。
        HTTPException ``status.HTTP_403_FORBIDDEN``:session 不属于当前 child。
    """
    session = (
        await db.execute(select(SessionModel).where(SessionModel.id == sid))
    ).scalar_one_or_none()

    if session is None or session.status == SessionStatus.deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SessionNotFound")

    if session.child_user_id != current.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "SessionForbidden")

    session.status = SessionStatus.deleted
    await db.commit()


@router.post("/sessions/{sid}/stop", status_code=204)
async def stop_session(
    sid: str,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """停止正在运行的对话流(best-effort),``POST /api/v1/me/sessions/{sid}/stop``。仅 child。

    向 ``running_streams[sid]`` 的 ``asyncio.Event`` 发 set,后台 LLM 任务
    下一次 yield 前会检测到并退出。无论 event 是否存在(流已自然结束、
    服务重启等)都返回 ``204``,best-effort 语义。

    Args:
        sid: session id(path param)。
        current: 当前账号上下文(``Depends(require_child)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。

    Returns:
        无。

    Raises:
        HTTPException ``status.HTTP_404_NOT_FOUND``:session 不存在或已软删。
        HTTPException ``status.HTTP_403_FORBIDDEN``:session 不属于当前 child。
    """
    session = (
        await db.execute(
            select(SessionModel).where(
                SessionModel.id == sid,
                SessionModel.status == SessionStatus.active,
            )
        )
    ).scalar_one_or_none()

    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SessionNotFound")

    if session.child_user_id != current.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "SessionForbidden")

    event = running_streams.get(sid)
    if event is not None:
        event.set()
    # 始终返回 204(异步 best-effort,后续清理由 pipeline 的 finally 块负责)


@router.post("/chat/stream")
async def chat_stream(
    request: Request,
    req: ChatStreamRequest,
    current: Annotated[CurrentAccount, Depends(require_child)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> StreamingResponse:
    """流式对话入口,``POST /api/v1/me/chat/stream``。仅 child。

    连接管理:前置 DB 工作(切日判定 / session 锁 / child profile 装配 /
    ``intake_human_message`` 决策矩阵 + commit)收在一个短作用域
    ``db_session_factory`` 块内,块退出即还连接到池,不横跨
    ``StreamingResponse``,不留 idle-in-transaction。DB 块与流式段各自有
    独立 try / except,``sid`` 显式初始化为 ``None`` 杜绝未绑定风险。

    流程:

    1. ``acquire_throttle_lock``:1.5s 节流,失败 ``429``
    2. 进入短作用域 DB 块:
       - 取最新 active session,按 ``should_switch_session``(自然日 R1 + 跨日 R2/R3)决定复用 / 新建
       - ``acquire_session_lock``:失败 ``409 SessionBusy``
       - 复用场景校验 session 归属
       - 读 ``ChildProfile`` 并构造 ``ChildProfileSnapshot``
       - ``intake_human_message`` 跑决策矩阵
       - 同步 ``last_active_at`` 并 commit
    3. 出 DB 块,进入流式段:
       - 构造 ``ChatContextSchema`` 与 LangGraph 初始 state
       - 注册 ``running_streams`` stop event(必须在 ``create_task`` 前)
       - ``asyncio.create_task`` 启动 ``run_llm_pipeline`` 后台任务
       - 返回 ``StreamingResponse``(``text/event-stream``),由 ``stream_generator``
         负责把 ``asyncio.Queue`` 中的 SSE 帧转发到客户端

    Args:
        request: FastAPI 请求对象,用于访问 ``app.state.resources``。
        req: ``ChatStreamRequest``(``content`` / ``session_id`` hint /
            ``regenerate_for``)。
        current: 当前账号上下文(``Depends(require_child)``)。
        redis: 主业务 Redis(``Depends(get_redis)``)。

    Returns:
        ``StreamingResponse``:``media_type="text/event-stream"``,承载
        LangGraph 流式产出的 SSE 事件。

    Raises:
        HTTPException ``status.HTTP_429_TOO_MANY_REQUESTS``:
            1.5s 节流窗口内重复请求。
        HTTPException ``status.HTTP_409_CONFLICT``:目标 session 已有
            其他 LLM 任务在跑(``SessionBusy``)。
        HTTPException ``status.HTTP_403_FORBIDDEN``:复用的 session 不
            属于当前 child(``SessionForbidden``)。
        HTTPException ``status.HTTP_404_NOT_FOUND``:
            ``ChildProfile`` 行不存在(``ChildProfileNotFound``)。
    """
    rr: RuntimeResources = request.app.state.resources

    # ---- throttle lock (TTL 自然过期,finally 不主动 DEL) ----
    if not await acquire_throttle_lock(redis, str(current.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "RequestThrottled")

    nonce: str | None = None
    sid: UUID | None = None

    # ==================================================================
    # 短作用域 DB 块:前置工作 + 锁获取 + user 消息落库
    # 块退出即还连接到池,不横跨 StreamingResponse
    # ==================================================================
    try:
        async with rr.db_session_factory() as db:
            # ---- session policy resolution(确定生效 sid) ----
            now = now_shanghai()
            latest = (
                await db.execute(
                    select(SessionModel)
                    .where(
                        SessionModel.child_user_id == current.id,
                        SessionModel.status == SessionStatus.active,
                    )
                    .order_by(SessionModel.last_active_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            session: SessionModel
            is_new_session: bool

            if should_switch_session(
                latest.last_active_at if latest else None,
                latest.created_at if latest else None,
                now,
            ):
                sid = uuid4()
                session = SessionModel(
                    id=sid,
                    child_user_id=current.id,
                    title=today_session_title(now),
                    status=SessionStatus.active,
                    last_active_at=now,
                )
                db.add(session)
                is_new_session = True
            else:
                assert latest is not None, "已有 active session"
                sid = latest.id
                session = latest
                is_new_session = False

            # ---- session lock ----
            nonce = await acquire_session_lock(redis, str(sid))
            if not nonce:
                raise HTTPException(status.HTTP_409_CONFLICT, "SessionBusy")

            # ---- session ownership check(仅复用 session 需验证) ----
            if not is_new_session and session.child_user_id != current.id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "SessionForbidden")

            # ---- 准备 child_profile 数据 ----
            child_profile = await load_child_profile(db, current.id)
            if child_profile is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "ChildProfileNotFound")

            profile_snapshot = ChildProfileSnapshot.from_profile(child_profile)

            # ---- 决策矩阵 + 首轮 / 续轮事务 ----
            result = await intake_human_message(db, sid, session, req)

            # user 消息落库(同事务内同步 last_active_at)
            if result.user_msg is not None:
                session.last_active_at = result.user_msg.created_at
            await db.commit()
    except HTTPException:
        # nonce 在 sid 之后赋值,非 None 时 sid 必已绑定
        if nonce is not None:
            await release_session_lock(redis, str(sid), nonce)
        raise
    except Exception:
        if nonce is not None:
            await release_session_lock(redis, str(sid), nonce)
        raise
    # ★ db 块退出 → 连接已还池

    # ==================================================================
    # 流式响应(不持 DB 连接)
    # ==================================================================
    try:
        from app.domain.chat.state import MainDialogueState

        ctx = ChatContextSchema(
            session_id=sid,
            child_user_id=current.id,
            child_profile=profile_snapshot,
            user_input=(
                result.regen_user_input if result.regen_user_input is not None else req.content
            ),
            settings=rr.settings,
            db_session_factory=rr.db_session_factory,
            audit_redis=rr.audit_redis,
            shared_http_client=rr.shared_http_client,
        )

        initial_state: MainDialogueState = {
            "messages": [],
            "audit_state": {
                "crisis_locked": False,
                "crisis_detected": False,
                "guidance": None,
                "target_message_id": None,
            },
            "turn_number": result.turn_number,
            "compression_summary": None,
            "keep_messages": None,
        }

        # stop event 必须在 create_task 之前完成注册,否则后台任务在注册
        # 完成前若已跑完第一段,client 后续 stop 调用查不到 event
        stop_event = asyncio.Event()
        running_streams[str(sid)] = stop_event

        _maxsize = rr.settings.chat_queue_maxsize
        queue: asyncio.Queue = asyncio.Queue(maxsize=_maxsize)
        state = ChatStreamState()

        bg = asyncio.create_task(
            run_llm_pipeline(
                rr=rr,
                redis=redis,
                sid=sid,
                hid=result.hid,
                nonce=nonce,
                child_user_id=current.id,
                turn_number=result.turn_number,
                initial_state=initial_state,
                ctx=ctx,
                queue=queue,
                state=state,
                stop_event=stop_event,
            ),
            name=f"chat-llm-{sid}",
        )
        rr.register_chat_task(str(sid), bg)

        return StreamingResponse(
            stream_generator(queue, state, sid),
            media_type="text/event-stream",
        )
    except HTTPException:
        await release_session_lock(redis, str(sid), nonce)
        raise
    except Exception:
        await release_session_lock(redis, str(sid), nonce)
        raise
