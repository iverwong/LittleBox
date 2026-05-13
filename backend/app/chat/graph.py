"""Main dialogue LangGraph — 5 nodes + 1 conditional router.

M6 Step 6: replaces the M3 single-node ChatState graph.
DB writes (persist_ai_turn) and audit enqueue (enqueue_audit) are
TOP-LEVEL HELPERS — NOT inside the graph.
T5 single-write-point = me.py generator (Step 8b); this file only
exports helpers for the generator to call.

Graph topology (baseline §7.1):
    START → load_audit_state → route_by_risk
                              ├─ "crisis"   → call_crisis_llm  → END
                              ├─ "redline"  → call_redline_llm → END
                              ├─ "guidance" → inject_guidance  → call_main_llm → END
                              └─ "main"    → call_main_llm      → END

5 risk signals → 4 routing outputs (baseline §7.1.1):
  ① crisis_locked=true (sticky, highest priority) → "crisis"
  ② crisis_detected=true                          → "crisis"
  ③ redline_triggered=true                         → "redline"
  ④ guidance != None                              → "guidance"  (pre-call_main_llm)
  ⑤ else (M6 default)                             → "main"
"""

import logging
import uuid

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.extractors import extract_finish_reason, extract_reasoning_content, extract_usage
from app.chat.factory import get_chat_llm
from app.chat.state import MainDialogueState
from app.models.chat import Message, Session
from app.models.enums import InterventionType, MessageRole, MessageStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: persistence + audit (called from me.py generator, NOT from graph)
# ---------------------------------------------------------------------------


async def persist_ai_turn(
    db: AsyncSession,
    sid: uuid.UUID,
    finish_reason: str,
    content: str,
    intervention_type: InterventionType | None = None,
) -> uuid.UUID:
    """Persist one AI turn as an active message row (M6-patch3: no longer updates last_active_at).

    T5 single-write-point: called from me.py generator after the stream ends.
    This helper does NOT touch the messages table inside the graph.
    last_active_at 由 commit① 独占（F 决策），commit② 不再覆写。

    Args:
        db: async DB session
        sid: session UUID
        finish_reason: LLM stop reason (stop / length / content_filter / user_stopped)
        content: accumulated text content
        intervention_type: None=normal, crisis=redline=guided=override type

    Returns:
        The id of the newly inserted AI message row (uuid.UUID).
    """
    msg = Message(
        session_id=sid,
        role=MessageRole.ai,
        content=content,
        status=MessageStatus.active,
        finish_reason=finish_reason,
        intervention_type=intervention_type,
    )
    db.add(msg)
    await db.flush()  # populate msg.id
    return msg.id


async def enqueue_audit(sid: uuid.UUID, db: AsyncSession) -> None:
    """Enqueue a session for async audit (M6 stub — no-op, logs warning).

    # TODO(M8): write to Redis audit-queue so M8 review worker picks it up.
    This is a placeholder; M6 has no review worker.
    """
    logger.warning("M6 stub: enqueue_audit called — no-op (M8 review worker pending)")


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


async def load_audit_state(state: MainDialogueState) -> dict:
    """Load audit signals from Redis and/or PG rolling_summaries.

    M6 stub: returns all-False audit state.
    M8: reads Redis audit:{sid} for this-turn signals AND
        queries rolling_summaries.crisis_locked for the sticky flag.

    # TODO(M8): replace body with Redis GET audit:{sid} + PG SELECT.
    """
    return {
        "audit_state": {
            "crisis_locked": False,
            "crisis_detected": False,
            "redline_triggered": False,
            "guidance": None,
        }
    }


def route_by_risk(state: MainDialogueState) -> str:
    """5 signals → 4 routing outputs (baseline §7.1.1).

    Priority: crisis_locked (① sticky) > crisis_detected (②) >
              redline_triggered (③) > guidance (④) > else (⑤ main)

    Args:
        state["audit_state"]: dict with keys crisis_locked / crisis_detected /
                              redline_triggered / guidance
    Returns:
        "crisis" | "redline" | "guidance" | "main"
    """
    audit = state.get("audit_state", {})
    if audit.get("crisis_locked") or audit.get("crisis_detected"):
        return "crisis"
    if audit.get("redline_triggered"):
        return "redline"
    if audit.get("guidance") is not None:
        return "guidance"
    return "main"


async def inject_guidance(state: MainDialogueState) -> dict:
    """Stage guidance text into pending_guidance field (M6 stub).

    M6: always writes None (no guidance in M6).
    M8: reads audit_state["guidance"] (non-None) and stages it.
         The actual injection into the LLM prompt is done in call_main_llm
         so that pending_guidance stays in-graph and NEVER touches
         the messages table (T5 single-write-point discipline).

    # TODO(M8): read guidance from audit_state and populate pending_guidance.
    """
    # M6: guidance always None — nothing to stage
    return {"pending_guidance": None}


def _assemble_llm_messages(state: MainDialogueState) -> list[BaseMessage]:
    """Assemble the list of messages to send to the LLM.

    - state["messages"] already contains [system_prompt, *history]
      built by the me.py generator (Step 8b).
    - If pending_guidance is set, insert a SystemMessage immediately
      before the last HumanMessage (baseline §7.5).
    - Does NOT modify state["messages"] (T5 discipline: guidance stays
      in-graph, never reaches the DB write path).
    """
    messages = list(state["messages"])  # shallow copy
    guidance = state.get("pending_guidance")
    if guidance:
        # Find the last HumanMessage and insert before it
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                messages.insert(i, SystemMessage(content=guidance))
                break
        else:
            # No HumanMessage found (e.g. first-turn empty history) — append
            messages.append(SystemMessage(content=guidance))
    return messages


async def call_main_llm(state: MainDialogueState) -> dict:
    """Call the main chat LLM, streaming chunks via get_stream_writer().

    LLM prompt assembly:
      - system prompt + history are already baked into state["messages"]
        by the me.py generator (Step 8b).
      - pending_guidance (if non-None) is injected as a SystemMessage
        immediately BEFORE the last HumanMessage in the list.
        This keeps state["messages"] (and thus the DB write path) clean.

    finish_reason passthrough: only white-list values
    (stop / length / content_filter) are forwarded; others fall through
    to the caller which emits "stop" as the default.

    No DB writes: persist_ai_turn is called from me.py generator after
    the stream ends (T5 single-write-point = generator).
    """
    writer = get_stream_writer()
    llm = get_chat_llm()
    parts: list[str] = []

    # Assemble LLM prompt (guidance injection without touching state["messages"])
    llm_messages = _assemble_llm_messages(state)

    provider = state.get("provider", "deepseek")

    async for chunk in llm.astream(llm_messages):
        # astream() yields AIMessageChunk at runtime despite BaseMessage type annotation
        _chunk_typed: AIMessageChunk = chunk  # type: ignore[assignment]

        # reasoning passthrough (signal only, no text, baseline §3.2)
        if extract_reasoning_content(_chunk_typed, provider):
            writer({"reasoning": True})

        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if text:
            writer({"delta": text})
            parts.append(text)

        # finish_reason passthrough (whitelist only, helper dispatch)
        fr = extract_finish_reason(_chunk_typed, provider)
        if fr:
            writer({"finish_reason": fr})

        # usage_metadata passthrough：末帧 usage-only chunk 由 SDK 自动注入
        if _chunk_typed.usage_metadata is not None:
            usage = extract_usage(_chunk_typed)
            if usage:
                writer({"usage_metadata": usage})

    return {"messages": [AIMessage(content="".join(parts))]}


async def call_crisis_llm(state: MainDialogueState) -> dict:
    """Crisis LLM stub (M6) — falls back to main LLM + warning.

    M9: replace stub body with the real crisis-intervention prompt + model.

    # TODO(M9): real crisis LLM invocation.
    """
    logger.warning("M6 stub: call_crisis_llm invoked — falling back to main LLM")
    return await call_main_llm(state)


async def call_redline_llm(state: MainDialogueState) -> dict:
    """Redline LLM stub (M6) — falls back to main LLM + warning.

    M9: replace stub body with the real redline-intervention prompt + model.

    # TODO(M9): real redline LLM invocation.
    """
    logger.warning("M6 stub: call_redline_llm invoked — falling back to main LLM")
    return await call_main_llm(state)


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

_builder = StateGraph(MainDialogueState)
_builder.add_node("load_audit_state", load_audit_state)
_builder.add_node("call_main_llm", call_main_llm)
_builder.add_node("call_crisis_llm", call_crisis_llm)
_builder.add_node("call_redline_llm", call_redline_llm)
_builder.add_node("inject_guidance", inject_guidance)

_builder.set_entry_point("load_audit_state")

# 5 signals -> 4 routing outputs (baseline §7.1.1 / §7.1.2)
_builder.add_conditional_edges(
    "load_audit_state",
    route_by_risk,
    {
        "crisis": "call_crisis_llm",
        "redline": "call_redline_llm",
        "guidance": "inject_guidance",
        "main": "call_main_llm",
    },
)

# guidance branch: inject_guidance is a pre-processor before call_main_llm
_builder.add_edge("inject_guidance", "call_main_llm")

# Three LLM nodes all terminate directly; no DB write nodes inside the graph
_builder.add_edge("call_main_llm", END)
_builder.add_edge("call_crisis_llm", END)
_builder.add_edge("call_redline_llm", END)

main_graph = _builder.compile()
