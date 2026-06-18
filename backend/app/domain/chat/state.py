"""Main dialogue graph state — MainDialogueState TypedDict.

M6 Step 6: replaces the M3 skeleton.
- messages: session history, managed by add_messages reducer (LangGraph)
- audit_state: M8 load_audit_state 节点读取 Redis audit:{sid} 填充
"""

from __future__ import annotations

import uuid
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages


class AuditState(TypedDict):
    """load_audit_state 节点输出的审查信号状态。"""

    crisis_locked: bool  # 危机锁定（sticky）
    crisis_detected: bool  # 本轮危机检测
    redline_triggered: bool  # 本轮红线检测
    guidance: str | None  # 引导注入文本
    target_message_id: uuid.UUID | None  # M9: 被审查的 ai_msg id（main 图 PG 兜底路径可空）


# ---- TypedDict ----


class MainDialogueState(TypedDict):
    """Per-turn state for the main dialogue LangGraph.

    Scalar fields (generated_token_count / client_alive / etc.) do NOT need
    Annotated reducers — LangGraph last-write-wins semantics suffices.
    Only ``messages`` needs add_messages (append-only history).
    """

    messages: Annotated[list[BaseMessage], add_messages]
    audit_state: AuditState  # M8: load_audit_state 节点填充
    generated_token_count: int
    client_alive: bool
    user_stop_requested: bool
    turn_number: int  # M8: 当前对话轮次，由 me.py 从 sessions.ai_turn_counter+1 填入
    # 压缩相关
    compression_summary: str | None
    keep_messages: list[BaseMessage] | None
