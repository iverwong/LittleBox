"""审查 LangGraph agentic loop。

三阶段(load_context → audit_llm_call ↔ tool loop → write_results):

    START → load_context → audit_llm_call → audit_tools
                             ├─ structured_output 非空 → write_results → END
                             └─ structured_output 为空 → audit_llm_call(loop)

tool_choice="auto" + system prompt 强约束 + post-processing 兜底:
DS/BL 两端在思考模式下均不支持 tool_choice="required" 或 "any",走 auto 是覆盖
主备两端的唯一可行写法。post-processing 在 LLM 未调 audit_output 时发起一次
追问,仍不调则降级 verdict=warn。

无参工厂 + 4 节点 Runtime[AuditContextSchema] DI:db_session_factory / settings /
max_iter 从 runtime.context 取,替代 closure 注入。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError
from typing_extensions import TypedDict

from app.core.history_xml import serialize_history_to_xml
from app.domain.audit.llm import build_audit_llm
from app.domain.audit.prompts import build_audit_system_prompt
from app.domain.audit.schemas import (
    AuditDimensionScores,
    AuditOutputSchema,
    ReplaceInNotes,
)
from app.domain.audit.usecase import write_audit_results
from app.domain.chat.context import load_recent_messages

if TYPE_CHECKING:
    from langgraph.runtime import Runtime
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.audit.context_schema import AuditContextSchema

logger = logging.getLogger("audit.graph")

TOOL_NAME_REPLACE = "ReplaceInNotes"
TOOL_NAME_OUTPUT = "AuditOutputSchema"


def _find_last_output_tool_call(messages: list[BaseMessage]) -> Any:
    """从 messages 历史反向查找最近的 AuditOutputSchema tool_call。

    用于 max_iter 兜底:从历史中拿 LLM 给过的最后一份 OUTPUT args 构造
    structured_output(强制清掉 guidance_injection 守"正常/无结论轮必须留空"契约)。

    Args:
        messages: LangChain 消息列表。

    Returns:
        最近一次 AuditOutputSchema tool_call;不存在则返回 None。
    """
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc["name"] == TOOL_NAME_OUTPUT:
                    return tc
    return None


class AuditGraphState(TypedDict):
    """审查图状态。

    `messages` 承载 LLM ↔ tool 循环累积,通过 ``add_messages`` reducer 自动追加。
    `load_context` 节点构造首帧 messages(含 system prompt + 历史对话 + 当前
    session_notes)。tool loop 中 audit_tools 返回的 ToolMessage 通过此 reducer
    追加到 messages 末尾。

    Attributes:
        sid: 被审查的对话 session ID(字符串形式)。
        turn_number: 本轮 ai_turn 编号。
        session_notes_working: 维护中的 session_notes 全文(working copy);
            初次由 load_context 从 PG 读取,audit_tools 通过 ReplaceInNotes
            逐步改写。
        tool_iter_count: 已完成的 tool 迭代次数,用于 max_iter 降级判断。
        structured_output: 终态结论;非空 → 路由到 write_results 并落库。
        messages: LangChain 消息列表,reducer = add_messages。
    """

    sid: str
    turn_number: int
    session_notes_working: str
    tool_iter_count: int
    structured_output: AuditOutputSchema | None
    messages: Annotated[list[BaseMessage], add_messages]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _last_aimessage(messages: list[BaseMessage]) -> AIMessage | None:
    """反向搜索最近的 AIMessage。

    Args:
        messages: LangChain 消息列表。

    Returns:
        最近一条 AIMessage;不存在则返回 None。
    """
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _build_audit_output_default(
    turn_summary: str,
    guidance_injection: str | None = None,
) -> AuditOutputSchema:
    """构造降级用的默认 AuditOutputSchema(循环超限 / post-processing 兜底)。

    Args:
        turn_summary: 摘要字段,降级时通常填诊断字符串(如「无该轮摘要(审查降级:…)」),
            会落 audit_records.turn_summary。
        guidance_injection: 默认 None;降级路径不向主 LLM 注入引导串,避免降级串
            被 route_by_risk 的 guidance 分支误投到孩子侧 prompt。

    Returns:
        全 0 维度评分 + 无 crisis + 指定摘要的 AuditOutputSchema。
    """
    return AuditOutputSchema(
        dimension_scores=AuditDimensionScores(
            emotional=0,
            social=0,
            values=0,
            boundaries=0,
            academic=0,
            lifestyle=0,
        ),
        crisis_detected=False,
        crisis_topic=None,
        guidance_injection=guidance_injection,
        turn_summary=turn_summary,
    )


async def _load_session_notes_from_pg(
    sid: UUID,
    db: AsyncSession,
) -> str:
    """从 PG 读该 session 的历史 `session_notes`(per-session 唯一行)。

    用于 load_context 跨轮注入:worker 层只 seed `session_notes_working=""`,
    真实历史 notes 需从 `RollingSummary` 表读,否则跨轮被静默覆盖。

    Args:
        sid: 被审查的对话 session UUID。
        db: 调用方传入的 AsyncSession(仅 SELECT,不 commit)。

    Returns:
        历史 session_notes;若记录不存在,返回空骨架(各栏目留空)。
    """
    from sqlalchemy import select

    from app.domain.audit.models import RollingSummary

    rs = await db.scalar(
        select(RollingSummary.session_notes).where(RollingSummary.session_id == sid).limit(1)
    )
    if rs is None:
        return """## 话题脉络

## 风险观察

## 情绪走向

## 待续关注

## 备注
"""
    return rs


# ---------------------------------------------------------------------------
# Node: load_context
# ---------------------------------------------------------------------------


async def load_context(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """从 PG 读近 4 轮消息 + 历史 session_notes + 构造首帧 messages。

    切分规则:最近 8 条(4 轮 H/A:from_turn=N-3 到 to_turn=N)拆成
    "前 3 轮(6 条)"和"当前 1 轮(2 条)"两段,分别用 serialize_history_to_xml
    包装为单条 HumanMessage 嵌入 prompt,避免 chat template 把多轮 H/A 末帧
    视为 generation prefix 触发续写。

    session_notes 从 PG 读 + seed 到 working copy:虽然 worker 入口 seed 空串,
    但实际历史由本节点注入,避免跨轮被静默覆盖。

    Args:
        state: 当前图状态(取 turn_number)。
        runtime: LangGraph Runtime,context 即 AuditContextSchema。

    Returns:
        含 messages(首帧 + SystemMessage + HumanMessage)与 session_notes_working 的 dict。
    """
    ctx = runtime.context
    sid = ctx.session_id
    async with ctx.db_session_factory() as db:
        history = await load_recent_messages(
            sid, db, state["turn_number"] - 3, state["turn_number"], as_orm=False
        )
        session_notes = await _load_session_notes_from_pg(sid, db)
    prior_turns = history[:-2]  # 前 3 轮 = 6 条消息
    current_turn = history[-2:]  # 当前 1 轮 = 2 条消息
    prior_xml = serialize_history_to_xml(prior_turns, include_system=False)
    current_xml = serialize_history_to_xml(current_turn, include_system=False)
    return {
        "messages": [
            build_audit_system_prompt(ctx.child_profile, ctx.max_iter),
            HumanMessage(
                content=(
                    f"以下是该会话最近 3 轮历史对话:\n{prior_xml}\n\n"
                    f"以下是当前轮次对话:\n{current_xml}\n\n"
                    f"当前 session_notes:\n<session_notes>{session_notes}</session_notes>"
                )
            ),
        ],
        "session_notes_working": session_notes,
    }


# ---------------------------------------------------------------------------
# Node: audit_llm_call
# ---------------------------------------------------------------------------


async def audit_llm_call(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """调审查 LLM + 纯文本追问兜底 + AIMessage 透传。

    责任:
    1. 调 LLM,等回复
    2. 若回复无 tool_calls,追问一轮(post-processing),仍无则 logger.warning
       记诊断(双失败降级 structured_output 由 audit_tools 防御性兜底设 default)
    3. 有 tool_calls 的回复一律透传,structured_output 不设 → audit_tools
       由 audit_tools 做规则校验(混/多 OUTPUT)、参数校验(pydantic)、单 OUTPUT 终止

    节点契约:本节点出参恒为 {"messages": [response]},不设 structured_output。
    审计图 audit_llm_call → audit_tools 直连,无 route_after_llm 中转。

    Args:
        state: 当前图状态(取 messages 累积)。
        runtime: LangGraph Runtime,context 即 AuditContextSchema。

    Returns:
        {"messages": [response]},AIMessage 透传,structured_output 不设。
    """
    ctx = runtime.context
    llm = build_audit_llm(ctx.settings, http_async_client=ctx.shared_http_client)
    messages = list(state["messages"])
    response = await llm.ainvoke(messages)

    # 协议违规 1:模型返回纯文本 → post-processing 追问
    if not response.tool_calls:
        messages.append(response)
        messages.append(
            HumanMessage(
                content="请调用 AuditOutputSchema 工具给出最终结论"
                "(verdict 为 pass / warn / fail),"
                "不要直接回复文本。你仍然可以在调用 AuditOutputSchema"
                " 之前先调用 ReplaceInNotes"
                " 更新记录笔记。",
            ),
        )
        response = await llm.ainvoke(messages)
        if not response.tool_calls:
            # 两次都未调 audit_output → 降级路径。
            # guidance_injection 不再传运营态字符串:降级时主 LLM 不应收到
            # 任何 guidance 注入(route_by_risk.ready 分支 guidance 为 None
            # → 落到 main 分支而非 guidance 分支)。
            # structured_output 由 audit_tools 防御性兜底(无 tool_call 路径)设 default。
            logger.warning("audit_pipeline: 模型连续两次未调用 audit_output，默认 verdict=warn")

    # 规则校验(混/多 OUTPUT)、参数校验(pydantic)、单 OUTPUT 终止由 audit_tools 负责
    # 本节点只把 AIMessage 透传,structured_output 不设
    return {"messages": [response]}


# ---------------------------------------------------------------------------
# Node: audit_tools
# ---------------------------------------------------------------------------


async def audit_tools(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """自写 ToolNode:规则校验 + 参数校验 + 笔记应用 + 单 OUTPUT 终止。

    职责(按 frame 分类):
    - 整帧单 OUTPUT(且 args 校验通过):终止信号。设 structured_output
      (按契约清掉 guidance_injection),不发 ToolMessage。路由到 write_results。
    - 整帧单 OUTPUT(args 校验失败):发 error ToolMessage(带字段级
      validation_errors),不设 structured_output。路由到 audit_llm_call 修正。
    - 混调 [NOTE, OUTPUT] / 多 OUTPUT:对每个这种 OUTPUT 发 error ToolMessage
      ("请单独调用一次 audit_output..."),应用 note 工具,不设 structured_output。
      路由到 audit_llm_call 修正。
    - 仅 note 工具:应用,返回 ok ToolMessage。不设 structured_output。
      路由到 audit_llm_call 让 LLM 继续。
    - 无 tool_calls:防御性兜底,设 default structured_output。路由到 write_results。
    - max_iter 超限:尾部兜底,设 default structured_output 并发出 ToolMessage 队列。

    audit_llm_call 出参恒为 {"messages": [response]}(无 structured_output),
    本节点承担所有校验 + 终止决策。

    Args:
        state: 当前图状态(取 messages / session_notes_working / tool_iter_count)。
        runtime: LangGraph Runtime,context 即 AuditContextSchema(max_iter)。

    Returns:
        dict 含 messages(ToolMessage 列表或空)与 session_notes_working / tool_iter_count
        更新;终态时附带 structured_output。
    """
    ctx = runtime.context
    max_iter = ctx.max_iter
    last_ai = _last_aimessage(state["messages"])
    # 防御性:audit_llm_call 一般情况下 last_ai.tool_calls 非空,如为空则路由到兜底策略
    if last_ai is None or not last_ai.tool_calls:
        return {
            "structured_output": _build_audit_output_default(
                turn_summary="无该轮摘要(审查降级:无工具调用)",
            ),
        }

    new_notes = state["session_notes_working"]
    payload: dict[str, Any] = {}

    # 单 OUTPUT 终止路径:整帧恰好 1 个 audit_output tool_call
    if len(last_ai.tool_calls) == 1 and last_ai.tool_calls[0]["name"] == TOOL_NAME_OUTPUT:
        tc = last_ai.tool_calls[0]
        tid = tc["id"]
        try:
            structured = AuditOutputSchema.model_validate(tc["args"])
        except ValidationError as exc:
            # 单 OUTPUT 但 args 非法:发 error ToolMessage(带字段级 validation_errors)
            # 触发下一轮 ainvoke 修正。不设 structured_output → 路由到 audit_llm_call。
            payload = {
                "error": "AuditOutputSchema args 校验失败，请按 schema 重发",
                "validation_errors": [
                    {
                        "loc": list(err["loc"]),
                        "msg": err["msg"],
                        "type": err["type"],
                    }
                    for err in exc.errors()
                ],
            }
            iter_count = state["tool_iter_count"] + 1
            return {
                "messages": [
                    ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False),
                        tool_call_id=tid,
                    )
                ],
                "session_notes_working": new_notes,
                "tool_iter_count": iter_count,
            }
        else:
            # 单 OUTPUT 校验通过:
            # 不发 ToolMessage —— audit_tools 出参 {"structured_output": structured}
            # 路由到 write_results;不增 tool_iter_count(终止信号,loop 结束)。
            return {"structured_output": structured}

    # 多 tool_call 路径:混调 / 多 OUTPUT / 纯 note
    tool_messages: list[ToolMessage] = []

    note_tcs = [tc for tc in last_ai.tool_calls if tc["name"] == TOOL_NAME_REPLACE]

    output_tcs = [tc for tc in last_ai.tool_calls if tc["name"] == TOOL_NAME_OUTPUT]

    last_note_tc = note_tcs[-1] if note_tcs else None

    if output_tcs:
        for tc in output_tcs:
            name, args, tid = tc["name"], tc["args"], tc["id"]
            # 混调 / 多 OUTPUT 违规:发 error ToolMessage,不解析
            payload = {
                "error": "请单独调用一次 AuditOutputSchema 给出最终结论，\
不要与笔记工具混调或重复调用"
            }
            tool_messages.append(
                ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=tid),
            )

    for tc in note_tcs:
        name, args, tid = tc["name"], tc["args"], tc["id"]
        is_last_note = tc is last_note_tc
        if name == TOOL_NAME_REPLACE:
            try:
                structured = ReplaceInNotes.model_validate(args)
            except ValidationError as exc:
                payload = {
                    "error": "ReplaceInNotes args 校验失败，请按 schema 重发",
                    "validation_errors": [
                        {
                            "loc": list(err["loc"]),
                            "msg": err["msg"],
                            "type": err["type"],
                        }
                        for err in exc.errors()
                    ],
                }
                tool_messages.append(
                    ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=tid),
                )
            else:
                old, new = args["old_str"], args["new_str"]
                count = new_notes.count(old)
                if count == 0:
                    payload = {"error": "old_str 未找到"}
                elif count >= 2:
                    payload = {"error": f"old_str 匹配 {count} 个, 扩展上下文使其唯一"}
                else:
                    new_notes = new_notes.replace(old, new)
                    payload = {"ok": True}
                if is_last_note:
                    payload["current_notes"] = new_notes
                tool_messages.append(
                    ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=tid),
                )
        else:
            logger.error(
                "audit.undefined_tool_call sid=%s turn=%s name=%s",
                state["sid"],
                state["turn_number"],
                name,
            )
            payload = {"error": f"未定义的 tool_call: {name}"}
            tool_messages.append(
                ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=tid),
            )

    iter_count = state["tool_iter_count"] + 1
    result_dict = {
        "messages": tool_messages,
        "session_notes_working": new_notes,
        "tool_iter_count": iter_count,
    }

    # max_iter 降级:尾部兜底
    if iter_count >= max_iter:
        logger.warning(
            "audit.loop_exceeded sid=%s turn=%s count=%d",
            state["sid"],
            state["turn_number"],
            iter_count,
        )

        result_dict["structured_output"] = _build_audit_output_default(
            turn_summary="无该轮摘要(审查降级:已超过迭代次数)"
        )
    return result_dict


# ---------------------------------------------------------------------------
# Route: route_after_tools
# ---------------------------------------------------------------------------


def route_after_tools(state: AuditGraphState) -> Literal["audit_llm_call", "write_results"]:
    """structured_output 非空 → write_results;否则 → audit_llm_call。

    structured_output 短路:若 audit_tools 已在 max_iter 兜底设了 structured_output,
    即使 iter_count 未到 max_iter,也直接 write_results。避免 [NOTE, OUTPUT] 路径下
    LLM 持续违规时无意义 loop。

    Args:
        state: 当前图状态(读 state.get("structured_output"))。

    Returns:
        "write_results" 或 "audit_llm_call"。
    """
    if state.get("structured_output") is not None:
        return "write_results"
    return "audit_llm_call"


# ---------------------------------------------------------------------------
# Node: write_results
# ---------------------------------------------------------------------------


async def write_results(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """从 state.structured_output 读取结果 → 落库。

    state.structured_output 由 audit_llm_call(单 OUTPUT 解析 / 双失败降级)
    或 audit_tools(max_iter 尾部兜底)保证非 None。保留 if output is not None
    防御,对 None 状态容错(理论上不可达但保留)。

    Args:
        state: 当前图状态(取 structured_output / sid / turn_number /
            session_notes_working)。
        runtime: LangGraph Runtime,context 即 AuditContextSchema
            (target_message_id / db_session_factory)。

    Returns:
        {"structured_output": output},供 ainvoke 最终结果回传。
    """
    output = state["structured_output"]
    if output is not None:
        ctx = runtime.context
        async with ctx.db_session_factory() as db:
            await write_audit_results(
                db=db,
                session_id=state["sid"],
                turn_number=state["turn_number"],
                structured_output=output,
                session_notes_final=state["session_notes_working"],
                turn_summary=output.turn_summary,
                target_message_id=ctx.target_message_id,
            )
            await db.commit()

    return {"structured_output": output}


# ---------------------------------------------------------------------------
# Graph builder(无参工厂 + context_schema=AuditContextSchema)
# ---------------------------------------------------------------------------


def build_audit_graph() -> CompiledStateGraph:
    """构建审查图,无参工厂。

    资源通过 Runtime[AuditContextSchema] DI 注入各节点;删除 max_iter / settings
    参数,context_schema=AuditContextSchema 开启 Runtime DI,各节点函数体内
    从 runtime.context 取资源。

    Returns:
        编译后的 CompiledStateGraph,可直接 ainvoke(state, context=audit_ctx)。
    """
    from app.domain.audit.context_schema import AuditContextSchema

    builder = StateGraph(AuditGraphState, context_schema=AuditContextSchema)

    builder.add_node("load_context", load_context)
    builder.add_node("audit_llm_call", audit_llm_call)
    builder.add_node("audit_tools", audit_tools)
    builder.add_node("write_results", write_results)

    builder.add_edge("__start__", "load_context")
    builder.add_edge("load_context", "audit_llm_call")
    # audit_llm_call → audit_tools 直连:
    # 节点契约保证 audit_tools 必收到 tool_calls(无 tool_calls 由 audit_tools 防御性兜底)
    builder.add_edge("audit_llm_call", "audit_tools")
    builder.add_conditional_edges(
        "audit_tools",
        route_after_tools,
        {"audit_llm_call": "audit_llm_call", "write_results": "write_results"},
    )
    builder.add_edge("write_results", "__end__")

    return builder.compile()  # type: ignore[reportReturnType]
