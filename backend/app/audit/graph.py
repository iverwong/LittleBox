"""审查 LangGraph agentic loop（M8 Step 5 / M8-hotfix Step 4）。

三阶段（load_context → audit_llm_call ↔ tool loop → write_results）：

    START → load_context → audit_llm_call → route_after_llm
                                           ├─ "audit_tools" → audit_tools → route_after_tools
                                           │                                ├─ "audit_llm_call" → audit_llm_call
                                           │                                └─ "write_results" → write_results → END
                                           └─ "write_results" → write_results → END

D11 v3（M8-hotfix）：tool_choice="auto" + system prompt 强约束 + post-processing 兜底。
DS/BL 两端在思考模式下均不支持 tool_choice="required" 或 "any"（36 变体穷尽实证），
走 auto 是覆盖主备两端的唯一可行写法。post-processing 在 LLM 未调 audit_output
时发起一次追问，仍不调则降级 verdict=warn。
"""
from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from app.audit.llm import build_audit_llm
from app.audit.prompts import build_audit_system_prompt
from app.audit.writers import write_audit_results
from app.schemas.audit import (
    AuditDimensionScores,
    AuditOutputSchema,
)

logger = logging.getLogger("audit.graph")

TOOL_NAME_APPEND = "AppendNote"
TOOL_NAME_REPLACE = "ReplaceInNotes"
TOOL_NAME_OUTPUT = "AuditOutputSchema"


def _has_audit_output(response: AIMessage) -> bool:
    """检查模型的响应中是否调用了 audit_output 工具（D11 v3 post-processing）。"""
    return any(tc["name"] == TOOL_NAME_OUTPUT for tc in (response.tool_calls or []))


class AuditGraphState(TypedDict):
    """审查图状态。

    `messages` 承载 LLM ↔ tool 循环累积，通过 ``add_messages`` reducer 自动追加。
    `load_context` 节点构造首帧 messages（含 system prompt + 历史对话 + 当前 session_notes）。
    tool loop 中 audit_tools 返回的 ToolMessage 通过此 reducer 追加到 messages 末尾。
    """

    sid: str
    turn_number: int
    child_profile: dict | None
    session_notes_working: str
    tool_iter_count: int
    structured_output: AuditOutputSchema | None
    messages: Annotated[list[BaseMessage], add_messages]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _last_aimessage(messages: list[BaseMessage]) -> AIMessage | None:
    """反向搜索最近的 AIMessage。"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _build_audit_output_default(
    guidance: str = "审查循环超限，已降级",
    turn_summary: str = "审查超时降级",
) -> AuditOutputSchema:
    """构造降级用的默认 AuditOutputSchema（循环超限 / post-processing 兜底）。"""
    return AuditOutputSchema(
        dimension_scores=AuditDimensionScores(
            emotional=0, social=0, romance=0, values=0,
            boundaries=0, academic=0, lifestyle=0,
        ),
        crisis_detected=False, crisis_topic=None,
        redline_triggered=False, redline_detail=None,
        guidance=guidance,
        turn_summary=turn_summary,
    )


# ---------------------------------------------------------------------------
# Node: load_context
# ---------------------------------------------------------------------------


async def _load_messages_from_pg(
    sid: str, limit: int = 10,
) -> list[BaseMessage]:
    """从 PG 读近 N 轮 messages 并转为 LangChain BaseMessage 列表。

    TODO(M9): 接入 rolling_summaries 闭环后，history 可包含压缩摘要。
    M8 期使用默认 limit=10 与主图对齐。
    """
    from app.db import _session_maker

    async with _session_maker() as db:
        from sqlalchemy import text

        rows = await db.execute(
            text(
                "SELECT role, content FROM messages "
                "WHERE session_id = :sid ORDER BY created_at DESC LIMIT :limit"
            ),
            {"sid": sid, "limit": limit},
        )
        msgs = rows.fetchall()

    result: list[BaseMessage] = []
    for role, content in reversed(msgs):
        if role == "human":
            result.append(HumanMessage(content=content))
        elif role == "ai":
            result.append(AIMessage(content=content))
    return result


async def load_context(state: AuditGraphState) -> dict:
    """从 PG 读近 N 轮消息 + 构造首帧 messages（system + history + session_notes）。"""
    history = await _load_messages_from_pg(state["sid"])
    return {
        "messages": [
            SystemMessage(content=build_audit_system_prompt()),
            *history,
            HumanMessage(content=f"当前 session_notes：\n{state['session_notes_working']}"),
        ],
    }


# ---------------------------------------------------------------------------
# Node: audit_llm_call
# ---------------------------------------------------------------------------


def _make_audit_llm_call(settings: Any):
    """工厂：创建 audit_llm_call 节点，settings 闭包注入。

    D11 v3（M8-hotfix）：tool_choice="auto" + post-processing 兜底。
    当模型返回纯文本（无 tool_calls）时，发起一次追问要求调 audit_output；
    再失败则降级 verdict=warn。中间工具调用（AppendNote/ReplaceInNotes）
    不走 post-processing，由 tool loop 正常路由。
    """
    async def audit_llm_call(state: AuditGraphState) -> dict:
        """调审查 LLM（三 tool + tool_choice="auto"）。"""
        llm = build_audit_llm(settings)
        messages = list(state["messages"])
        response = await llm.ainvoke(messages)

        # ---- D11 v3 post-processing 兜底 ----
        # 仅在模型返回纯文本（无任何 tool_calls）时触发；
        # 模型若调了中间工具（AppendNote/ReplaceInNotes），由 tool loop 继续迭代
        if not response.tool_calls:
            messages.append(response)
            messages.append(
                HumanMessage(
                    content="请调用 audit_output 工具给出最终结论"
                    "（verdict 为 pass / warn / fail），"
                    "不要直接回复文本。你仍然可以在调用 audit_output"
                    " 之前先调用 append_note 或 replace_in_notes"
                    " 记录笔记。",
                )
            )
            response = await llm.ainvoke(messages)

            if not response.tool_calls:
                # 两次都未调 audit_output → 降级
                logger.warning(
                    "audit_pipeline: 模型连续两次未调用 audit_output，默认 verdict=warn"
                )
                return {
                    "messages": [response],
                    "structured_output": _build_audit_output_default(
                        guidance="模型未能给出结构化结论",
                        turn_summary="审查降级：模型未调用 audit_output",
                    ),
                }

        return {"messages": [response]}
    return audit_llm_call


# ---------------------------------------------------------------------------
# Route: route_after_llm
# ---------------------------------------------------------------------------


def route_after_llm(state: AuditGraphState) -> Literal["audit_tools", "write_results"]:
    """按 tool_call.name 路由：AuditOutputSchema → 终止，否则继续 tool loop。"""
    last = _last_aimessage(state["messages"])
    if last is None or not last.tool_calls:
        return "write_results"
    tc = last.tool_calls[0]
    if tc["name"] == TOOL_NAME_OUTPUT:
        return "write_results"
    return "audit_tools"


# ---------------------------------------------------------------------------
# Node: audit_tools
# ---------------------------------------------------------------------------


async def audit_tools(state: AuditGraphState) -> dict:
    """自写 ToolNode：处理 AppendNote / ReplaceInNotes，D9 协议返回 JSON 序列化 ToolMessage。

    超限时（tool_iter_count >= max_iter）构造降级 AuditOutputSchema 并强制退出 loop。
    """
    max_iter: int = 5  # 默认值，由 build_audit_graph closure 覆盖
    return await _audit_tools_impl(state, max_iter)


async def _audit_tools_impl(state: AuditGraphState, max_iter: int) -> dict:
    """audit_tools 内部实现，max_iter 由 closure 注入（build_audit_graph 设定）。"""
    last_ai = _last_aimessage(state["messages"])
    if last_ai is None or not last_ai.tool_calls:
        return {}

    tool_messages: list[ToolMessage] = []
    new_notes = state["session_notes_working"]

    for tc in last_ai.tool_calls:
        name, args, tid = tc["name"], tc["args"], tc["id"]

        if name == TOOL_NAME_APPEND:
            new_notes = new_notes + ("\n" if new_notes else "") + args["text"]
            payload = {"ok": True, "current_notes": new_notes}

        elif name == TOOL_NAME_REPLACE:
            old, new = args["old_str"], args["new_str"]
            count = new_notes.count(old)
            if count == 0:
                payload = {"error": "old_str not found", "current_notes": new_notes}
            elif count >= 2:
                payload = {
                    "error": f"old_str matches {count} times, please extend context to make it unique",
                    "current_notes": new_notes,
                }
            else:
                new_notes = new_notes.replace(old, new, 1)
                payload = {"ok": True, "current_notes": new_notes}
        else:
            payload = {"error": f"unknown tool: {name}", "current_notes": new_notes}

        tool_messages.append(
            ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=tid),
        )

    iter_count = state["tool_iter_count"] + 1

    # 循环超限降级
    if iter_count >= max_iter:
        logger.warning(
            "audit.loop_exceeded sid=%s turn=%s count=%d",
            state["sid"], state["turn_number"], iter_count,
        )
        # 最后一条未应用 tool_call 的内容 append 到 notes
        last_tc = last_ai.tool_calls[-1]
        fallback_text = last_tc["args"].get("text") or last_tc["args"].get("new_str", "")
        if fallback_text:
            new_notes += f"\n[审查 agent 多次尝试修改未果，原始建议如下]\n{fallback_text}"
        return {
            "messages": tool_messages,
            "session_notes_working": new_notes,
            "tool_iter_count": iter_count,
            "structured_output": _build_audit_output_default(),
        }

    return {
        "messages": tool_messages,
        "session_notes_working": new_notes,
        "tool_iter_count": iter_count,
    }


# ---------------------------------------------------------------------------
# Route: route_after_tools
# ---------------------------------------------------------------------------


def _make_route_after_tools(max_iter: int):
    """工厂：创建 route_after_tools，max_iter 由 build_audit_graph 注入。"""

    def route_after_tools(state: AuditGraphState) -> Literal["audit_llm_call", "write_results"]:
        """超限强制退出 loop → write_results；否则返回 audit_llm_call。"""
        if state["tool_iter_count"] >= max_iter:
            logger.warning(
                "audit.loop_exceeded sid=%s turn=%s iter=%d max=%d",
                state["sid"], state["turn_number"], state["tool_iter_count"], max_iter,
            )
            return "write_results"
        return "audit_llm_call"

    return route_after_tools


# ---------------------------------------------------------------------------
# Node: write_results
# ---------------------------------------------------------------------------


async def write_results(state: AuditGraphState) -> dict:
    """从 structured_output 或 messages 末尾 AIMessage 提取结果 → 落库。"""
    output = state["structured_output"]
    if output is None:
        last_ai = _last_aimessage(state["messages"])
        if last_ai and last_ai.tool_calls:
            output = AuditOutputSchema.model_validate(last_ai.tool_calls[0]["args"])

    if output is not None:
        from app.db import _session_maker

        async with _session_maker() as db:
            await write_audit_results(
                db=db,
                session_id=state["sid"],
                turn_number=state["turn_number"],
                structured_output=output,
                session_notes_final=state["session_notes_working"],
                turn_summary=output.turn_summary,
            )

    return {"structured_output": output}


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_audit_graph(max_iter: int = 5, settings: Any = None) -> StateGraph:
    """构建审查图，闭包注入各节点/路由函数。

    Args:
        max_iter: tool agentic loop 硬上限（默认 5）。
        settings: Settings 实例，用于构造 LLM。传递后 build_audit_llm 在节点内被调用；
                  不传则以默认方式获取 settings（用于测试 monkeypatch mock LLM）。
    """
    async def _audit_tools_with_max_iter(state: AuditGraphState) -> dict:
        return await _audit_tools_impl(state, max_iter)

    _audit_llm_call = _make_audit_llm_call(settings)
    _route_after_tools = _make_route_after_tools(max_iter)

    builder = StateGraph(AuditGraphState)

    builder.add_node("load_context", load_context)
    builder.add_node("audit_llm_call", _audit_llm_call)
    builder.add_node("audit_tools", _audit_tools_with_max_iter)
    builder.add_node("write_results", write_results)

    builder.add_edge("__start__", "load_context")
    builder.add_edge("load_context", "audit_llm_call")
    builder.add_conditional_edges(
        "audit_llm_call",
        route_after_llm,
        {"audit_tools": "audit_tools", "write_results": "write_results"},
    )
    builder.add_conditional_edges(
        "audit_tools",
        _route_after_tools,
        {"audit_llm_call": "audit_llm_call", "write_results": "write_results"},
    )
    builder.add_edge("write_results", END)

    return builder.compile()
