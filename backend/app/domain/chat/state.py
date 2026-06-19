"""Main dialogue graph state —— MainDialogueState TypedDict。

- messages: session 历史,由 add_messages reducer(LangGraph)管理
- audit_state: load_audit_state 节点读取 Redis audit:{sid} 填充
- turn_number: 当前对话轮次,由 me.py 从 sessions.ai_turn_counter+1 填入
- compression_summary / keep_messages: 图内压缩节点使用的辅助字段
"""

from __future__ import annotations

import uuid
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages


class AuditState(TypedDict):
    """load_audit_state 节点输出的审查信号状态。

    Attributes:
        crisis_locked: 危机锁定,sticky 黏住(触发后持续到图内解锁逻辑重置)。
        crisis_detected: 本轮危机检测结果。
        guidance: 引导注入文本(红线详情也合并至此字段)。
        target_message_id: 被审查的 ai_msg id(main 图 PG 兜底路径可空)。
    """

    crisis_locked: bool
    crisis_detected: bool
    guidance: str | None
    target_message_id: uuid.UUID | None


# ---- TypedDict ----


class MainDialogueState(TypedDict):
    """Per-turn state for the main dialogue LangGraph。

    标量字段不需要 Annotated reducer —— LangGraph last-write-wins 语义足够。
    仅 ``messages`` 需要 add_messages(append-only 历史)。

    Attributes:
        messages: 会话历史,由 LangGraph add_messages reducer 追加。
        audit_state: load_audit_state 节点读取 Redis audit 信号后填充。
        turn_number: 当前对话轮次(由 me.py 从 sessions.ai_turn_counter+1 填入)。
        compression_summary: 当前 active 压缩摘要,供主对话 system prompt 注入。
        keep_messages: 压缩后保留的最近 N 对消息(供 graph 节点拼装 history)。
    """

    messages: Annotated[list[BaseMessage], add_messages]
    audit_state: AuditState
    turn_number: int
    # 压缩相关
    compression_summary: str | None
    keep_messages: list[BaseMessage] | None
