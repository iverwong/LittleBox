"""build_context：全量 active 对话历史，供 LLM 输入。

职责边界：
- 本函数返回对话历史列表：
  [SystemMessage（rolling-summary，M8 可选）, HumanMessage, AIMessage, ...]
  按 created_at 升序排列，返回全量 active 消息（无 LIMIT 截断）。
- 返回列表**不含**主 system prompt。主 prompt（身份 / 安全 / 分级 / 性别 / 年龄）
  由 `prompts.build_system_prompt(age, gender)` 独立生成。
  调用方拼接：[build_system_prompt(...), *build_context(...), HumanMessage(user_content)]

5 项语义约束：
  1. 仅返回 status='active' 的消息（discarded 行被过滤）
  2. 按 created_at ASC 排序，无 LIMIT
  3. rolling_summaries 在 M6 为只读路径（始终 fall through）；M8 后 turn_summaries
     非空时注入 SystemMessage 在列表首位（fallthrough 路径）
  4. session_notes 永不注入主 LLM（架构基线 §四「字段消费分工」）
  5. 未知 role 兜底转为 HumanMessage（防御性）

调用方模式：
    system = build_system_prompt(age=child.age, gender=child.gender)
    history = await build_context(session_id, db)
    llm_messages = [system, *history, HumanMessage(content=new_message)]
"""
# TODO(M8 cleanup)：rolling_summaries fallback 替换为真实摘要注入
#   M8 review worker 上线后此文件无需改动；当前 fallback 丢弃摘要上下文。

from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.prompts import ANCHOR_WINDOW_PREFIX, SUMMARY_PREFIX
from app.core.config import settings
from app.core.enums import MessageRole
from app.models.audit import RollingSummary
from app.models.chat import Message


async def _load_active_messages(
    sid: UUID,
    db: AsyncSession,
    *,
    until_turn: int | None = None,
) -> list[Message]:
    """底层共享 helper：查询 session 中 status='active' 的消息。

    Args:
        until_turn: 非 None 时只返回 turn_number < until_turn 的行
                    （用于 main W1 装配链，排除本轮 human）
    """
    stmt = select(Message).where(Message.session_id == sid, Message.status == "active")
    if until_turn is not None:
        stmt = stmt.where(Message.turn_number < until_turn)
    stmt = stmt.order_by(Message.created_at.asc())
    return list((await db.execute(stmt)).scalars())


async def load_active_history_for_assembly(
    sid: UUID,
    current_turn: int,
    db: AsyncSession,
) -> list[BaseMessage]:
    """main W1 wrapper 装配链专用：返回不含本轮 human 的历史 + turn_summaries 前缀。

    与 build_context 的职责边界：
    - build_context: audit 路径专用，含本轮 human，不注入 turn_summaries
    - load_active_history_for_assembly: main W1 装配链专用，不含本轮 human，
      前缀含 turn_summaries SystemMessage 列表
    """
    rs = await db.scalar(select(RollingSummary).where(RollingSummary.session_id == sid).limit(1))
    summaries: list[SystemMessage] = []
    if rs and rs.turn_summaries:
        for s in rs.turn_summaries:
            text = f"Turn {s.get('turn_number', '?')}: {s.get('summary', '')}"
            summaries.append(SystemMessage(content=text))

    rows = await _load_active_messages(sid, db, until_turn=current_turn)
    return [*summaries, *(_to_lc_message(m) for m in rows)]


async def build_context(sid: UUID, db: AsyncSession) -> list[BaseMessage]:
    """返回 sid 所有 active 消息，按 created_at ASC，无 LIMIT。

    - 过滤条件：status='active'（discarded 行排除）
    - 排序：created_at ASC（全量返回）
    - rolling_summaries：M6 只读不回写；M8 当 turn_summaries 非空时，
      将 SystemMessage 注入列表首位
    - session_notes：永不注入主 LLM
    """
    rows = await _load_active_messages(sid, db)
    messages: list[BaseMessage] = [_to_lc_message(m) for m in rows]

    # rolling_summaries：M6 只读路径，始终 fall through
    # （M8 review worker 写入后改由非空 turn_summaries 触发注入）
    sm_stmt = select(RollingSummary.turn_summaries).where(RollingSummary.session_id == sid).limit(1)
    row = (await db.execute(sm_stmt)).scalar_one_or_none()

    # scalar_one_or_none()：None=无行；[]=空列表（二者均 falsy → fallback）
    if row:  # 非空列表 → M8 fallthrough；空列表 [] 为 falsy → fallback
        summary_text = "\n".join(f"Turn {s['turn']}: {s['summary']}" for s in row)
        messages.insert(0, SystemMessage(content=summary_text))

    return messages


# ---------------------------------------------------------------------------
# M9 三级干预上下文装配函数（§D）
# ---------------------------------------------------------------------------


async def load_recent_active_pairs(
    sid: UUID,
    current_turn: int,
    db: AsyncSession,
    n: int,
) -> list[BaseMessage]:
    """取当前轮之前最近 n 对 active human/ai 消息，按 turn 升序返回。

    SQL：WHERE turn_number < current_turn AND status='active'
    ORDER BY turn_number DESC LIMIT n*2 → Python reversed() 升序。
    """
    rows = (
        (
            await db.execute(
                select(Message)
                .where(
                    Message.session_id == sid,
                    Message.turn_number < current_turn,
                    Message.status == "active",
                )
                .order_by(Message.turn_number.desc())
                .limit(n * 2)
            )
        )
        .scalars()
        .all()
    )
    return [_to_lc_message(m) for m in reversed(rows)]


async def build_crisis_context(
    sid: UUID,
    db: AsyncSession,
    target_message_id: UUID,
) -> tuple[SystemMessage, list[BaseMessage]]:
    """crisis 上下文装配：anchor_window（绕 status） + after_anchor（仅 active）。

    anchor_window：anchor 及其之前 N 对（2N 条），绕过 status 过滤
    （物理原文段，不应被压缩/丢弃截断）。
    after_anchor：anchor 之后所有 active 行，不限条数。

    Returns:
        (anchor_system, after_anchor)
        - anchor_system: SystemMessage(content="[anchor 窗口]\\nrole: content\\n...")
        - after_anchor: 剩余 active 消息列表（HumanMessage/AIMessage）
    """
    anchor = await db.scalar(select(Message).where(Message.id == target_message_id))
    if anchor is None:
        raise ValueError(f"crisis anchor not found: {target_message_id}")

    n = settings.crisis_context_recent_turns  # 默认 5 对

    # anchor_window：绕 status，以 created_at 切分
    aw_rows = (
        (
            await db.execute(
                select(Message)
                .where(
                    Message.session_id == sid,
                    Message.created_at <= anchor.created_at,
                )
                .order_by(Message.created_at.desc())
                .limit(n * 2)
            )
        )
        .scalars()
        .all()
    )
    anchor_text_lines = [f"{m.role.value}: {m.content}" for m in reversed(aw_rows)]
    anchor_system = SystemMessage(
        content=ANCHOR_WINDOW_PREFIX + "\n" + "\n".join(anchor_text_lines)
    )

    # after_anchor：仅 active，anchor 之后
    after_rows = (
        (
            await db.execute(
                select(Message)
                .where(
                    Message.session_id == sid,
                    Message.created_at > anchor.created_at,
                    Message.status == "active",
                )
                .order_by(Message.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return anchor_system, [_to_lc_message(m) for m in after_rows]


async def build_redline_context(
    sid: UUID,
    current_turn: int,
    db: AsyncSession,
) -> tuple[list[SystemMessage], list[BaseMessage]]:
    """红线上下文装配：turn_summaries 前缀 + 最近 active 对。

    Returns:
        (summaries_systems, recent_pairs)
        - summaries_systems: 最近 redline_turn_summaries_window 条摘要的 SystemMessage 列表
        - recent_pairs: 最近 redline_context_recent_turns 对 active 消息
    """
    rs = await db.scalar(select(RollingSummary).where(RollingSummary.session_id == sid).limit(1))
    summaries: list[SystemMessage] = []
    if rs and rs.turn_summaries:
        # 取最近 redline_turn_summaries_window 条
        recent = rs.turn_summaries[-settings.redline_turn_summaries_window :]
        for s in recent:
            text = f"Turn {s.get('turn_number', '?')}: {s.get('summary', '')}"
            summaries.append(SystemMessage(content=text))

    pairs = await load_recent_active_pairs(
        sid,
        current_turn,
        db,
        n=settings.redline_context_recent_turns,
    )
    return summaries, pairs


# ---- LangChain 消息转换 ----


def _to_lc_message(m: Message) -> BaseMessage:
    """将 Message ORM 对象转换为 LangChain 消息。"""
    if m.role == MessageRole.human:
        return HumanMessage(content=m.content)
    if m.role == MessageRole.ai:
        return AIMessage(content=m.content)
    if m.role == MessageRole.summary:
        return SystemMessage(content=SUMMARY_PREFIX + m.content)
    # 防御性兜底：未知 role → HumanMessage 防止崩溃
    return HumanMessage(content=m.content)
