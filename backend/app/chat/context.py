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

from app.models.audit import RollingSummary
from app.models.chat import Message
from app.models.enums import MessageRole
from app.chat.prompts import SUMMARY_PREFIX


async def build_context(sid: UUID, db: AsyncSession) -> list[BaseMessage]:
    """返回 sid 所有 active 消息，按 created_at ASC，无 LIMIT。

    - 过滤条件：status='active'（discarded 行排除）
    - 排序：created_at ASC（全量返回）
    - rolling_summaries：M6 只读不回写；M8 当 turn_summaries 非空时，
      将 SystemMessage 注入列表首位
    - session_notes：永不注入主 LLM
    """
    rows = await db.execute(
        select(Message)
        .where(Message.session_id == sid, Message.status == "active")
        .order_by(Message.created_at.asc())
    )

    messages: list[BaseMessage] = [_to_lc_message(m) for m in rows.scalars().all()]

    # rolling_summaries：M6 只读路径，始终 fall through
    # （M8 review worker 写入后改由非空 turn_summaries 触发注入）
    sm_stmt = (
        select(RollingSummary.turn_summaries)
        .where(RollingSummary.session_id == sid)
        .limit(1)
    )
    row = (await db.execute(sm_stmt)).scalar_one_or_none()

    # scalar_one_or_none()：None=无行；[]=空列表（二者均 falsy → fallback）
    if row:  # 非空列表 → M8 fallthrough；空列表 [] 为 falsy → fallback
        summary_text = "\n".join(f"Turn {s['turn']}: {s['summary']}" for s in row)
        messages.insert(0, SystemMessage(content=summary_text))

    return messages


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
