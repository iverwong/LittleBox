"""对话历史查询 helper：供 LangGraph 节点装配 LLM 输入。

本模块只提供消息查询与 LangChain 转换函数，不组装 system prompt
（由 prompts.build_system_prompt 独立生成）。

5 项语义约束：
  1. 仅返回 status='active' 的消息（discarded 行被过滤）
  2. 按 created_at ASC 排序，无 LIMIT
  3. summary 行由 load_active_messages_with_summary 单独提取
     供 build_system_prompt 注入（不进 history，避免双写）
  4. session_notes 永不注入主 LLM（架构基线 §四「字段消费分工」）
  5. 未知 role 兜底转为 HumanMessage（防御性）
"""
# TODO(M8 cleanup)：rolling_summaries fallback 替换为真实摘要注入
#   M8 review worker 上线后此文件无需改动；当前 fallback 丢弃摘要上下文。

from typing import Literal, overload
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MessageRole, MessageStatus
from app.domain.chat.models import Message
from app.domain.chat.prompts import SUMMARY_PREFIX


async def load_active_messages_with_summary(
    sid: UUID, db: AsyncSession, *, from_turn: int | None = None, to_turn: int | None = None
) -> tuple[list[Message], Message | None]:
    """取 sid 当前 active 的 summary 消息(0 或 1 条)。

    M8 压缩产物:每次压缩在 messages 表插入一条 role=summary, status=active 的行,
    旧的 active summary 在新一轮压缩时被标 compressed —— 故任何时刻 active summary
    至多 1 条。该消息专供 main W1 wrapper 注入 build_system_prompt 用,
    不进 history(避免双写,见 compression 路径注释)。
    """
    stmt = select(Message).where(
        Message.session_id == sid,
        Message.status == MessageStatus.active,
    )
    if from_turn is not None:
        stmt = stmt.where(Message.turn_number >= from_turn)
    if to_turn is not None:
        stmt = stmt.where(Message.turn_number <= to_turn)
    stmt = stmt.order_by(Message.created_at.asc(), Message.id.asc())
    rows = list((await db.execute(stmt)).scalars())
    messages: list[Message] = []
    summary: Message | None = None
    for m in rows:
        if m.role == MessageRole.summary:
            summary = m
        else:
            messages.append(m)
    return messages, summary


# ---- LangChain 消息转换 ----
@overload
async def load_recent_messages(
    sid: UUID, db: AsyncSession, from_turn: int, to_turn: int, *, as_orm: Literal[False]
) -> list[BaseMessage]: ...


@overload
async def load_recent_messages(
    sid: UUID, db: AsyncSession, from_turn: int, to_turn: int, *, as_orm: Literal[True]
) -> list[Message]: ...


async def load_recent_messages(
    sid: UUID, db: AsyncSession, from_turn: int, to_turn: int, *, as_orm: bool = False
) -> list[BaseMessage] | list[Message]:
    """取 ``[from_turn, to_turn]`` 范围内 human/ai 消息（按 created_at 升序）。

    语义：返回指定 turn_number 闭区间内的所有 human/ai 消息（无 LIMIT 截断），
    调用方按需自行取末尾 n 条 / 倒推偏移。``from_turn``/``to_turn`` 均为
    闭区间端点（SQL BETWEEN 语义）。

    SQL：WHERE turn_number BETWEEN from_turn AND to_turn
        AND role IN ('human','ai')
    ORDER BY created_at ASC, id ASC → Python 升序。
    """
    rows = (
        (
            await db.execute(
                select(Message)
                .where(
                    Message.session_id == sid,
                    Message.turn_number.between(from_turn, to_turn),
                    Message.role.in_([MessageRole.human, MessageRole.ai]),
                    Message.status != MessageStatus.discarded,
                )
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if as_orm:
        return list(rows)
    return [to_lc_message(m) for m in rows]


# ---- LangChain 消息转换 ----


def to_lc_message(m: Message) -> BaseMessage:
    """将 Message ORM 对象转换为 LangChain 消息。"""
    if m.role == MessageRole.human:
        return HumanMessage(content=m.content)
    if m.role == MessageRole.ai:
        return AIMessage(content=m.content)
    if m.role == MessageRole.summary:
        return SystemMessage(content=SUMMARY_PREFIX + m.content)
    # 防御性兜底：未知 role → HumanMessage 防止崩溃
    return HumanMessage(content=m.content)
