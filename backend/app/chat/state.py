"""Main dialogue graph state — MainDialogueState TypedDict.

M6 Step 6: replaces the M3 skeleton.
Architecture (baseline §7.1 / §7.5):
- messages: session history, managed by add_messages reducer (LangGraph)
- pending_guidance: staging field written by inject_guidance node,
  consumed by call_main_llm to assemble the LLM prompt.
  NOT persisted — stays in-graph only.
- audit_state: M6 all-False stub; M8 reads Redis audit:{sid}
  TODO(M8) anchors: load_audit_state node, inject_guidance node, enqueue_audit helper
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

from app.models.accounts import (
    ChildProfile,  # runtime import: needed by LangGraph get_type_hints to resolve forward ref
)

# ---- TypedDict ----


class MainDialogueState(TypedDict):
    """Per-turn state for the main dialogue LangGraph.

    Scalar fields (session_id / child_user_id / etc.) do NOT need
    Annotated reducers — LangGraph last-write-wins semantics suffices.
    Only ``messages`` needs add_messages (append-only history).
    """

    session_id: str
    child_user_id: str
    child_profile: ChildProfile | None  # set by generator (Step 8b); M6 nodes do not read
    messages: Annotated[list[BaseMessage], add_messages]
    audit_state: dict  # M6: all False; M8: read Redis audit:{sid} + PG rolling_summaries
    pending_guidance: str | None  # staging — not persisted, not written to messages table
    generated_token_count: int
    client_alive: bool
    user_stop_requested: bool
