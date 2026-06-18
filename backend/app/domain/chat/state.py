"""Main dialogue graph state — MainDialogueState TypedDict.

- messages: session history, managed by add_messages reducer (LangGraph)
- audit_state: load_audit_state 节点读取 Redis audit:{sid} 填充
- turn_number: 当前对话轮次，由 me.py 从 sessions.ai_turn_counter+1 填入
- compression_summary / keep_messages: 图内压缩节点使用的辅助字段
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
    guidance: str | None  # 引导注入文本（红线详情也合并至此字段）
    target_message_id: uuid.UUID | None  # M9: 被审查的 ai_msg id（main 图 PG 兜底路径可空）


# ---- TypedDict ----


class MainDialogueState(TypedDict):
    """Per-turn state for the main dialogue LangGraph.

    Scalar fields do NOT need Annotated reducers —
    LangGraph last-write-wins semantics suffices.
    Only ``messages`` needs add_messages (append-only history).
    """

    messages: Annotated[list[BaseMessage], add_messages]
    audit_state: AuditState  # load_audit_state 节点填充
    turn_number: int  # 当前对话轮次，由 me.py 从 sessions.ai_turn_counter+1 填入
    # 压缩相关
    compression_summary: str | None
    keep_messages: list[BaseMessage] | None
