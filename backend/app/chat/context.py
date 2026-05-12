"""build_context: 全量 active 对话历史，供 LLM 输入。

M6 always falls through (read-only rolling_summaries, never writes).
M8 review worker auto-consumes pre-inserted summaries; this file needs no changes.

Responsibility boundary:
- This function returns a *dialogue history* list
  [SystemMessage(rolling-summary, optional, M8 only), HumanMessage, AIMessage, ...]
  in chronological (ascending) order, all active messages (no LIMIT).
- It is NOT the main system prompt. The main prompt (identity/safety/tier/gender/context)
  is produced by `prompts.build_system_prompt(age, gender)` independently.
  Callers concatenate: [build_system_prompt(...), *build_context(...), HumanMessage(user_content)]

Usage (caller-side pattern):
    system = build_system_prompt(age=child.age, gender=child.gender)
    history = await build_context(session_id, db)
    llm_messages = [system, *history, HumanMessage(content=new_message)]
"""
# TODO(M8 cleanup): replace rolling_summaries fallback with real summary injection
#   when M8 review worker is live; the fallback currently drops summary context.

from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import RollingSummary
from app.models.chat import Message
from app.models.enums import MessageRole


async def build_context(sid: UUID, db: AsyncSession) -> list[BaseMessage]:
    """Return all active messages for sid in chronological order.

    - Filters to status='active' only (discarded rows are excluded).
    - Orders by created_at ASC (no LIMIT — full history).
    - rolling_summaries is read-only in M6; when turn_summaries is non-empty
      a SystemMessage is prepended (M8 fallthrough path; not exercised in M6).
    - session_notes is never read or injected into the main LLM.
    """
    rows = await db.execute(
        select(Message)
        .where(Message.session_id == sid, Message.status == "active")
        .order_by(Message.created_at.asc())
    )

    messages: list[BaseMessage] = [_to_lc_message(m) for m in rows.scalars().all()]

    # Read-only rolling_summaries in M6 — always fall through
    # (M8 review worker will have inserted turn_summaries rows)
    sm_stmt = (
        select(RollingSummary.turn_summaries)
        .where(RollingSummary.session_id == sid)
        .limit(1)
    )
    row = (await db.execute(sm_stmt)).scalar_one_or_none()

    # scalar_one_or_none(): None when row absent, empty list [] when column is NULL or empty
    if row:  # non-empty list → M8 fallthrough; empty list [] is falsy → fallback
        summary_text = "\n".join(f"Turn {s['turn']}: {s['summary']}" for s in row)
        messages.insert(0, SystemMessage(content=summary_text))

    return messages


def _to_lc_message(m: Message) -> BaseMessage:
    """将 Message ORM 对象转换为 LangChain 消息。"""
    if m.role == MessageRole.human:
        return HumanMessage(content=m.content)
    if m.role == MessageRole.ai:
        return AIMessage(content=m.content)
    # Defensive: unknown role → treat as human to avoid crashes
    return HumanMessage(content=m.content)
