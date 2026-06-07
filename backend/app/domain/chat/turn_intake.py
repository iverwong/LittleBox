"""commit① 决策矩阵 + TurnIntakeResult 载体。

Phase 2.4 从 `api/me.py::chat_stream` 抽离。决策矩阵 7 row 行为完全
等价 (Row 1/2/3/4/5/6/7,见 `tests/api/test_chat_stream_control_plane.py`),
仅把矩阵执行结果通过 `TurnIntakeResult` dataclass 暴露给调用方。

外提变量(消费方):
- `hid` —— human 消息 ID,用于 session_meta 事件
- `user_msg` —— 本轮新增的 human message(用于 commit① last_active_at 同步)
- `regen_user_input` —— Row 6 复用孤儿行时喂入 ctx.user_input 的原始文本
- `protected_id` —— 压缩保护 id(Row 1 为 None,其他为 user_msg.id 或 last_msg.id)
- `turn_number` —— M9 turn 号,commit① human + commit② ai 共享同号
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.chat.schemas import ChatStreamRequest
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from app.models.enums import MessageRole, MessageStatus


@dataclass
class TurnIntakeResult:
    """决策矩阵 7 row 的执行结果,供 chat_stream 路由消费。"""

    hid: UUID
    user_msg: Message | None
    regen_user_input: str | None
    protected_id: UUID | None
    turn_number: int


async def intake_human_message(
    db: AsyncSession,
    sid: UUID,
    session: SessionModel,
    req: ChatStreamRequest,
) -> TurnIntakeResult:
    """commit① 决策矩阵 (baseline §5.4, 7 rows)。

    Row 1: last=None   + regen=null  → INSERT human (active) [session resolved via policy]
    Row 2: last=None   + regen=!null → 400 RegenerateForInvalid
    Row 3: last=AI     + regen=null  → INSERT human (active)
    Row 4: last=AI     + regen=!null → 400 RegenerateForInvalid
    Row 5: last=orphan + regen=null  → UPDATE old discarded + INSERT human (active)
    Row 6: last=orphan + regen=hid   → reuse orphan (no new row, content must be "")
    Row 7: last=orphan + regen=!hid → 400 RegenerateForInvalid

    Gate A 闭合论证(适用 Row 5-7):
    "Last active message" = SELECT ... WHERE status='active'
    ORDER BY created_at DESC, id DESC LIMIT 1 — 永远是最新 active 行。
    "非孤儿 human" 意味着有一条 active AI 行严格排在它之后——但该 AI 行本身
    会成为"最新 active 行",与 ORDER BY 结果矛盾。
    因此 "last active row is human" ⟺ "orphan human";无需二次查询。
    Rows 8/9 (non-orphan human paths) 不可达。
    """
    last_msg = (
        await db.execute(
            select(Message)
            .where(Message.session_id == sid, Message.status == MessageStatus.active)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # M9: turn_number = 下一轮号(commit① human + commit② ai 共享同号)
    turn_number = (session.ai_turn_counter or 0) + 1

    hid: UUID  # human 消息 ID,用于 session_meta 事件
    user_msg: Message | None = None  # 追踪本轮新增的 human message,供 commit① 用
    # Row 6 复用孤儿行时,从 last_msg.content 取值给 ctx.user_input:
    #   孤儿 turn_number == turn_number(AI 没落库 → ai_turn_counter 未自增),
    #   load_active_history_for_assembly(until_turn=turn_number) 会按 < turn_number
    #   过滤把孤儿排掉;W1 末位 HumanMessage 用 ctx.user_input 拼装,若不喂原始
    #   问题文本则 LLM 收到空 user 轮(仅在 regenerate 路径出现,Row 1/3/5 无影响)。
    regen_user_input: str | None = None

    if last_msg is None:
        # Row 1 或 Row 2
        if req.regenerate_for is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "RegenerateForInvalid")
        # Row 1:首轮(INSERT human active;session 已在策略解析中建好)
        human = Message(
            session_id=sid,
            role=MessageRole.human,
            status=MessageStatus.active,
            content=req.content,
            turn_number=turn_number,
        )
        db.add(human)
        await db.flush()
        hid = human.id
        user_msg = human
    elif last_msg.role == MessageRole.ai:
        # Row 3:末条为 AI,regen=null → INSERT human
        # Row 4:regen=!null → 400
        if req.regenerate_for is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "RegenerateForInvalid")
        human = Message(
            session_id=sid,
            role=MessageRole.human,
            status=MessageStatus.active,
            content=req.content,
            turn_number=turn_number,
        )
        db.add(human)
        await db.flush()
        hid = human.id
        user_msg = human
    else:
        # last_msg.role == MessageRole.human
        is_orphan = last_msg.role == MessageRole.human

        if is_orphan:
            # Row 5、6、7
            if req.regenerate_for is None:
                # Row 5:孤儿 + null → UPDATE 旧行 discarded + INSERT 新行
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
                    turn_number=turn_number,
                )
                db.add(new_human)
                await db.flush()
                hid = new_human.id
                user_msg = new_human
            elif req.regenerate_for == str(last_msg.id):
                # Row 6:孤儿 + =hid → 复用孤儿行(不新增行,不更新内容)
                if req.content != "":
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "RegenerateForInvalid")
                hid = last_msg.id
                # user_msg 保持 None — 复用已有消息,不新增
                regen_user_input = last_msg.content  # 喂入 ctx.user_input(见上方注释)
            else:
                # Row 7:孤儿 + ≠hid → 400
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "RegenerateForInvalid")
        else:
            # 非孤儿 human 不可能成为末条 active 行(Gate A 闭合论证)。
            # 此分支不可达——raise 以捕获未来状态空间错误。
            raise AssertionError("unreachable: non-orphan human cannot be last active row")

    # Row 1 (last_msg is None) 无旧消息可压缩,protected_id 置 None
    protected_id: UUID | None = None
    if last_msg is not None:
        protected_id = user_msg.id if user_msg is not None else last_msg.id

    return TurnIntakeResult(
        hid=hid,
        user_msg=user_msg,
        regen_user_input=regen_user_input,
        protected_id=protected_id,
        turn_number=turn_number,
    )
