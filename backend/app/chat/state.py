"""Main dialogue graph state — MainDialogueState TypedDict.

M6 Step 6: replaces the M3 skeleton.
Architecture (baseline §7.1 / §7.5):
- messages: session history, managed by add_messages reducer (LangGraph)
- pending_guidance: staging field written by inject_guidance node,
  consumed by call_main_llm to assemble the LLM prompt.
  NOT persisted — stays in-graph only.
- audit_state: M8 load_audit_state 节点读取 Redis audit:{sid} 填充
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

from app.models.accounts import (
    ChildProfile,  # runtime import: needed by LangGraph get_type_hints to resolve forward ref
)


class AuditState(TypedDict):
    """load_audit_state 节点输出的审查信号状态。"""
    crisis_locked: bool       # 危机锁定（sticky，M9 才会真写）
    crisis_detected: bool     # 本轮危机检测
    redline_triggered: bool   # 本轮红线检测
    guidance: str | None      # 引导注入文本


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
    provider: str  # 当前对话 provider 名，由 me.py 从 settings.main_provider 填入
    messages: Annotated[list[BaseMessage], add_messages]
    audit_state: AuditState  # M8: load_audit_state 节点填充
    pending_guidance: str | None  # staging — not persisted, not written to messages table
    generated_token_count: int
    client_alive: bool
    user_stop_requested: bool
    turn_number: int  # M8: 当前对话轮次，由 me.py 从 sessions.ai_turn_counter+1 填入
