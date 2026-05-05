"""build_context: sliding-window dialogue history for LLM input.

M6 always falls through (read-only rolling_summaries, never writes).
M8 review worker auto-consumes pre-inserted summaries; this file needs no changes.

Responsibility boundary:
- This function returns a *dialogue history* list
  [SystemMessage(rolling-summary, optional, M8 only), HumanMessage, AIMessage, ...]
  in chronological (ascending) order, max 20 most-recent active messages.
- It is NOT the main system prompt. The main prompt (identity/safety/tier/gender/context)
  is produced by `prompts.build_system_prompt(age, gender)` independently.
  Callers concatenate: [build_system_prompt(...), *build_context(...), HumanMessage(user_content)]

Usage (caller-side pattern):
    system = build_system_prompt(age=child.age, gender=child.gender)
    history = await build_context(session_id, db)
    llm_messages = [system, *history, HumanMessage(content=new_message)]
"""

from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import RollingSummary
from app.models.chat import Message
from app.models.enums import MessageRole, MessageStatus


async def build_context(session_id: UUID, db: AsyncSession) -> list[BaseMessage]:
    """Return the last 20 active messages for session_id in chronological order.

    - Filters to status='active' only (discarded rows are excluded).
    - Orders by created_at DESC, takes top 20, then reverses to ascending.
    - rolling_summaries is read-only in M6; when turn_summaries is non-empty
      a SystemMessage is prepended (M8 fallthrough path; not exercised in M6).
    - session_notes is never read or injected into the main LLM.
    """
    # Active messages, last 20, reversed to chronological
    stmt = (
        select(Message.role, Message.content)
        .where(Message.session_id == session_id, Message.status == MessageStatus.active)
        .order_by(Message.created_at.desc())
        .limit(20)
    )
    rows = (await db.execute(stmt)).all()

    messages: list[BaseMessage] = [_row_to_message(r) for r in reversed(rows)]

    # Read-only rolling_summaries in M6 — always fall through
    # (M8 review worker will have inserted turn_summaries rows)
    sm_stmt = (
        select(RollingSummary.turn_summaries)
        .where(RollingSummary.session_id == session_id)
        .limit(1)
    )
    row = (await db.execute(sm_stmt)).scalar_one_or_none()

    if row:  # row is the turn_summaries list or None
        summaries: list[dict] = row
        if summaries:
            # M8 fallthrough: prepend rolling summary as a SystemMessage
            summary_text = "\n".join(f"Turn {s['turn']}: {s['summary']}" for s in summaries)
            messages.insert(0, SystemMessage(content=summary_text))

    return messages


def _row_to_message(row) -> BaseMessage:
    """Convert a DB row to a LangChain message."""
    role, content = row.role, row.content
    if role == MessageRole.human:
        return HumanMessage(content=content)
    if role == MessageRole.ai:
        return AIMessage(content=content)
    # Defensive: unknown role → treat as human to avoid crashes
    return HumanMessage(content=content)
