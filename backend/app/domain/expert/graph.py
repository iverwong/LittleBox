"""Expert LangGraph agentic loop。

四阶段(load_context → expert_llm_call ↔ expert_tools → write_results):

    START → load_context → expert_llm_call → expert_tools
                             ├─ structured_output 非空 → write_results → END
                             └─ structured_output 为空 → expert_llm_call(loop)

无 ReplaceInNotes(比 audit graph 更简单)。三工具:SearchHistoryInput、
FetchByRefInput(数据检索)、ExpertReportSchema(结构化输出)。工具循环中完成
token 预算检查、max_attempts 降级。

无参工厂 + 4 节点 Runtime[ExpertContextSchema] DI:db_session_factory / settings /
max_output_attempts / token_budget 从 runtime.context 取,替代 closure 注入。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date
from typing import TYPE_CHECKING, Annotated, Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typing_extensions import TypedDict

from app.core.enums import DailyStatus
from app.domain.expert.llm import build_expert_llm
from app.domain.expert.prompts import (
    _CrisisMarkerItem,
    _RecentReportOverviewItem,
    _TodayRollingSummaryItem,
    build_expert_first_human_message,
    build_expert_system_prompt,
)
from app.domain.expert.schemas import ExpertReportSchema
from app.domain.expert.tools import EXPERT_TOOL_HANDLERS
from app.domain.expert.usecase import write_expert_results

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

    from app.domain.expert.context_schema import ExpertContextSchema

logger = logging.getLogger("expert.graph")

TOOL_NAME_OUTPUT = "ExpertReportSchema"
TOOL_NAME_SEARCH = "SearchHistoryInput"
TOOL_NAME_FETCH = "FetchByRefInput"
_DATA_TOOL_NAMES = {TOOL_NAME_SEARCH, TOOL_NAME_FETCH}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class ExpertGraphState(TypedDict):
    """专家图状态。

    `messages` 承载 LLM ↔ tool 循环累积,通过 ``add_messages`` reducer 自动追加。
    `load_context` 节点构造首帧 messages(含 system prompt + 今日材料)。
    tool loop 中 expert_tools 返回的 ToolMessage 通过此 reducer 追加到 messages。

    Attributes:
        messages: LangChain 消息列表,reducer = add_messages。
        output_attempts: 已尝试的 ExpertReportSchema 提交次数。
        total_output_tokens: LLM 累计产出 token 数(含 retry 和追问)。
        structured_output: 终态结论;非空 → 路由到 write_results 并落库。
        _budget_forced: 内部标记,是否已注入 token 预算催缴消息。
    """

    messages: Annotated[list[BaseMessage], add_messages]
    output_attempts: int
    total_output_tokens: int
    structured_output: ExpertReportSchema | None
    _budget_forced: bool


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


def _build_degraded_output(crisis_detected_today: bool = False) -> ExpertReportSchema:
    """构造降级用的 ExpertReportSchema(循环超限 / post-processing 兜底)。

    Args:
        crisis_detected_today: 当日是否有 crisis 标记,影响降级 status 取值。

    Returns:
        各段内容填写降级说明的 ExpertReportSchema。
    """
    status = DailyStatus.alert if crisis_detected_today else DailyStatus.attention
    msg = "报告生成降级：系统未能完成正常分析流程，请稍后重试或联系客服"
    return ExpertReportSchema(
        overall_status=status,
        degraded=True,
        today_overview=msg,
        what_was_discussed=msg,
        emotion_changes=msg,
        noteworthy=msg,
        suggestions=msg,
        anomaly_periods=msg,
    )


async def _fetch_recent_reports(
    db_session_factory: async_sessionmaker[AsyncSession],
    child_user_id: uuid.UUID,
    exclude_date: date,
    limit: int = 5,
) -> list[_RecentReportOverviewItem]:
    """查近 limit 条历史报告概要(原 worker._get_recent_reports,移入 graph.py)。

    Args:
        db_session_factory: DB 会话工厂。
        child_user_id: 孩子用户 ID。
        exclude_date: 报告日期(不包含)。
        limit: 返回上限,默认 5。

    Returns:
        list[dict],每项含 report_date / overall_status / today_overview。
        无数据时返回空 list。
    """
    from app.domain.expert.models import DailyReport

    async with db_session_factory() as db:
        stmt = (
            select(
                DailyReport.report_date,
                DailyReport.overall_status,
                DailyReport.today_overview,
            )
            .where(
                DailyReport.child_user_id == child_user_id,
                DailyReport.report_date < exclude_date,
            )
            .order_by(DailyReport.report_date.desc())
            .limit(limit)
        )
        rows = (await db.execute(stmt)).all()
    return [
        {
            "report_date": r.report_date,
            "overall_status": r.overall_status,
            "today_overview": r.today_overview,
        }
        for r in rows
    ]


async def _fetch_today_materials(
    db_session_factory: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
) -> tuple[_TodayRollingSummaryItem | None, _CrisisMarkerItem | None]:
    """查今日对话材料:today_summary + crisis 标记。

    Args:
        db_session_factory: DB 会话工厂。
        session_id: 今日 chat session ID。

    Returns:
        (today_summary, crisis_marker) 元组。无数据时两个 list 都为空。
    """
    from app.domain.audit.models import AuditRecord, RollingSummary

    async with db_session_factory() as db:
        rs_row = (
            await db.execute(
                select(RollingSummary).where(
                    RollingSummary.session_id == session_id,
                )
            )
        ).scalar_one_or_none()
        ar_row = (
            await db.execute(
                select(AuditRecord)
                .where(AuditRecord.session_id == session_id, AuditRecord.crisis_detected.is_(True))
                .order_by(AuditRecord.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()

    crisis_marker: _CrisisMarkerItem | None = None
    today_summary: _TodayRollingSummaryItem | None = None

    if rs_row:
        today_summary = _TodayRollingSummaryItem(
            session_id=str(rs_row.session_id),
            turn_summaries=[
                {
                    "turn_number": entry["turn_number"],
                    "summary": entry["summary"],
                    "time": entry["time"],
                }
                for entry in (rs_row.turn_summaries or [])
            ],
            session_notes=rs_row.session_notes or "",
        )

    if ar_row:
        crisis_marker = _CrisisMarkerItem(
            session_id=str(ar_row.session_id),
            turn_number=ar_row.turn_number,
            crisis_topic=ar_row.crisis_topic or "",
        )

    return today_summary, crisis_marker


# ---------------------------------------------------------------------------
# Node: load_context
# ---------------------------------------------------------------------------


async def load_context(
    state: ExpertGraphState,
    runtime: Runtime[ExpertContextSchema],
) -> dict:
    """构造首帧 messages: system prompt + 含材料的 HumanMessage。

    锚定 `ctx.session_id`(由 worker 强制 1:1 invariant,本节点无需再校验),
    通过 `_fetch_today_materials` 取单条 today_summary + crisis_marker。

    Args:
        state: 当前图状态(空,首次运行)。
        runtime: LangGraph Runtime,context 即 ExpertContextSchema。

    Returns:
        含 messages(首帧 system + human)与其余状态字段的 dict。
    """
    ctx = runtime.context

    # 1. 抓数据(helper 内起短 DB session)
    recent_reports = await _fetch_recent_reports(
        ctx.db_session_factory,
        ctx.child_user_id,
        ctx.report_date,
    )
    today_summary, crisis_marker = await _fetch_today_materials(
        ctx.db_session_factory,
        ctx.session_id,
    )

    # 2. 拼首帧
    return {
        "messages": [
            build_expert_system_prompt(ctx.max_output_attempts),
            build_expert_first_human_message(
                report_date=ctx.report_date,
                recent_reports_overview=recent_reports,
                today_summary=today_summary,
                crisis_marker=crisis_marker,
            ),
        ],
        "output_attempts": 0,
        "total_output_tokens": 0,
        "structured_output": None,
        "_budget_forced": False,
    }


# ---------------------------------------------------------------------------
# Node: expert_llm_call
# ---------------------------------------------------------------------------


async def expert_llm_call(
    state: ExpertGraphState,
    runtime: Runtime[ExpertContextSchema],
) -> dict:
    """调专家 LLM + 纯文本追问兜底 + AIMessage 透传 + token 累计。

    责任:
    1. 调 LLM,等回复
    2. 累计 total_output_tokens
    3. 若回复无 tool_calls,追问一轮(post-processing),仍无则 logger.warning
       记诊断(双失败降级 structured_output 由 expert_tools 防御性兜底设 degraded)
    4. 有 tool_calls 的回复一律透传,structured_output 不设 → expert_tools
       由 expert_tools 做规则校验(混/多 OUTPUT)、参数校验(pydantic)、单 OUTPUT 终止

    节点契约:本节点出参恒为 {"messages": [response], "total_output_tokens": new_total},
    不设 structured_output。专家图 expert_llm_call → expert_tools 直连,
    无 route_after_llm 中转。

    Args:
        state: 当前图状态(取 messages 累积、total_output_tokens)。
        runtime: LangGraph Runtime,context 即 ExpertContextSchema。

    Returns:
        {"messages": [response], "total_output_tokens": new_total},AIMessage 透传。
    """
    ctx = runtime.context
    llm = build_expert_llm(ctx.settings, http_async_client=ctx.shared_http_client)
    messages = list(state["messages"])
    response = await llm.ainvoke(messages)

    # 累计 token
    token_usage = response.response_metadata.get("token_usage", {})
    output_tokens = token_usage.get("output_tokens", 0)
    new_total = state["total_output_tokens"] + output_tokens

    # 协议违规:模型返回纯文本 → post-processing 追问
    if not response.tool_calls:
        messages.append(response)
        messages.append(
            HumanMessage(
                content="请调用 ExpertReportSchema 工具给出最终报告,"
                "不要直接回复文本。你仍然可以在调用 ExpertReportSchema"
                " 之前先调用 SearchHistoryInput 或 FetchByRefInput 检索数据。",
            ),
        )
        response = await llm.ainvoke(messages)

        # 累计追问 token
        token_usage = response.response_metadata.get("token_usage", {})
        output_tokens = token_usage.get("output_tokens", 0)
        new_total += output_tokens

        if not response.tool_calls:
            # 两次都未调 ExpertReportSchema → 降级路径。
            # structured_output 由 expert_tools 防御性兜底(无 tool_call 路径)设 degraded。
            logger.warning("expert_pipeline: 模型连续两次未调用 ExpertReportSchema，降级")

    return {"messages": [response], "total_output_tokens": new_total}


# ---------------------------------------------------------------------------
# Node: expert_tools
# ---------------------------------------------------------------------------


async def expert_tools(
    state: ExpertGraphState,
    runtime: Runtime[ExpertContextSchema],
) -> dict:
    """自写 ToolNode:规则校验 + 参数校验 + 数据检索 + 单 OUTPUT 终止。

    职责(按 frame 分类):
    - 整帧单 OUTPUT(且 args 校验通过):终止信号。设 structured_output,
      不发 ToolMessage。路由到 write_results。
    - 整帧单 OUTPUT(args 校验失败):发 error ToolMessage(带字段级
      validation_errors),不设 structured_output。路由到 expert_llm_call 修正。
    - 混调/多 OUTPUT:对每个 OUTPUT 发 error ToolMessage("请单独调用一次
      ExpertReportSchema..."),不设 structured_output。路由到 expert_llm_call 修正。
    - 仅数据工具(SearchHistoryInput/FetchByRefInput):调 EXPERT_TOOL_HANDLERS,
      挂 token 预算检查,返回 ToolMessage。不设 structured_output。
      路由到 expert_llm_call 让 LLM 继续。
    - 无 tool_calls:防御性兜底,设 degraded structured_output。路由到 write_results。
    - output_attempts >= max_output_attempts:尾部兜底,设 degraded structured_output。

    expert_llm_call 出参恒为 {"messages": [response]}(无 structured_output),
    本节点承担所有校验 + 终止决策。

    Args:
        state: 当前图状态(取 messages / output_attempts / total_output_tokens /
            _budget_forced)。
        runtime: LangGraph Runtime,context 即 ExpertContextSchema
            (token_budget / max_output_attempts)。

    Returns:
        dict 含 messages(ToolMessage 列表或空)与 output_attempts 更新;
        终态时附带 structured_output。
    """
    ctx = runtime.context
    last_ai = _last_aimessage(state["messages"])
    # 防御性:expert_llm_call 一般情况下 last_ai.tool_calls 非空,如为空则路由到兜底
    if last_ai is None or not last_ai.tool_calls:
        return {
            "structured_output": _build_degraded_output(
                crisis_detected_today=ctx.crisis_detected_today,
            ),
        }

    # --- Token 预算检查 ---
    budget_forced = state.get("_budget_forced", False)
    budget_exceeded = state["total_output_tokens"] >= ctx.token_budget
    force_msg_sent = False

    # 分类 tool_calls
    output_tcs = [tc for tc in last_ai.tool_calls if tc["name"] == TOOL_NAME_OUTPUT]
    data_tcs = [tc for tc in last_ai.tool_calls if tc["name"] in _DATA_TOOL_NAMES]
    other_tcs = [
        tc
        for tc in last_ai.tool_calls
        if tc["name"] not in _DATA_TOOL_NAMES and tc["name"] != TOOL_NAME_OUTPUT
    ]

    # --- 单 OUTPUT 终止路径:整帧恰好 1 个 output + 无 data + 无 other ---
    if len(output_tcs) == 1 and not data_tcs and not other_tcs:
        tc = output_tcs[0]
        tid = tc["id"]
        try:
            structured = ExpertReportSchema.model_validate(tc["args"])
        except ValidationError as exc:
            payload = {
                "error": "ExpertReportSchema args 校验失败,请按 schema 重发",
                "validation_errors": [
                    {
                        "loc": list(err["loc"]),
                        "msg": err["msg"],
                        "type": err["type"],
                    }
                    for err in exc.errors()
                ],
            }
            return {
                "messages": [
                    ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False),
                        tool_call_id=tid,
                    )
                ],
                "output_attempts": state["output_attempts"] + 1,
            }
        else:
            # 单 OUTPUT 校验通过:不发 ToolMessage,直接终止
            return {"structured_output": structured}

    # --- 多 tool_call 路径 ---
    tool_messages: list[ToolMessage | HumanMessage] = []

    # 预算已超且尚未催缴 → 注入强制交卷 HumanMessage
    if budget_exceeded and not budget_forced:
        tool_messages.append(
            HumanMessage(
                content="你已收集了大量材料，token 预算接近上限，"
                "请立即调用 ExpertReportSchema 提交最终报告。"
            ),
        )
        force_msg_sent = True

    # 处理其他(未定义)工具
    for tc in other_tcs:
        name, tid = tc["name"], tc["id"]
        logger.error("expert.undefined_tool_call name=%s", name)
        tool_messages.append(
            ToolMessage(
                content=json.dumps(
                    {"error": f"未定义的 tool_call: {name}"},
                    ensure_ascii=False,
                ),
                tool_call_id=tid,
            ),
        )

    # 处理 OUTPUT 违规(混调/多 OUTPUT)
    for tc in output_tcs:
        tid = tc["id"]
        tool_messages.append(
            ToolMessage(
                content=json.dumps(
                    {
                        "error": "请单独调用一次 ExpertReportSchema 给出最终报告，"
                        "不要与数据检索工具混调或重复调用"
                    },
                    ensure_ascii=False,
                ),
                tool_call_id=tid,
            ),
        )

    # 处理数据工具调用
    for tc in data_tcs:
        name, args, tid = tc["name"], tc["args"], tc["id"]
        handler = EXPERT_TOOL_HANDLERS.get(name)
        if handler is None:
            logger.error("expert.no_handler name=%s", name)
            tool_messages.append(
                ToolMessage(
                    content=json.dumps(
                        {"error": f"handler not found: {name}"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tid,
                ),
            )
        elif budget_exceeded and not force_msg_sent:
            # 预算已超:拒绝更多数据检索
            tool_messages.append(
                ToolMessage(
                    content=json.dumps(
                        {"error": "token 预算已超限,请立即调用 ExpertReportSchema"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tid,
                ),
            )
        else:
            tool_msg = await handler(args, runtime, tid)
            tool_messages.append(tool_msg)

    result_dict: dict[str, Any] = {
        "messages": tool_messages,
        "_budget_forced": force_msg_sent or budget_forced,
    }

    # --- Max attempts 尾部兜底 ---
    new_output_attempts = state["output_attempts"] + len(output_tcs)
    result_dict["output_attempts"] = new_output_attempts

    if new_output_attempts >= ctx.max_output_attempts:
        logger.warning(
            "expert.max_attempts_exceeded child=%s attempts=%d",
            ctx.child_user_id,
            new_output_attempts,
        )
        result_dict["structured_output"] = _build_degraded_output(
            crisis_detected_today=ctx.crisis_detected_today,
        )

    return result_dict


# ---------------------------------------------------------------------------
# Route: route_after_tools
# ---------------------------------------------------------------------------


def route_after_tools(
    state: ExpertGraphState,
) -> Literal["expert_llm_call", "write_results"]:
    """structured_output 非空 → write_results;否则 → expert_llm_call。

    structured_output 短路:若 expert_tools 已在 max_attempts 兜底设了
    structured_output,即使未到 max_attempts,也直接 write_results。

    Args:
        state: 当前图状态(读 state.get("structured_output"))。

    Returns:
        "write_results" 或 "expert_llm_call"。
    """
    if state.get("structured_output") is not None:
        return "write_results"
    return "expert_llm_call"


# ---------------------------------------------------------------------------
# Node: write_results
# ---------------------------------------------------------------------------


async def write_results(
    state: ExpertGraphState,
    runtime: Runtime[ExpertContextSchema],
) -> dict:
    """从 state.structured_output 读取结果 → 落库。

    二层 overall_status 兜底:若 ctx.crisis_detected_today=True 但 LLM
    产出非 alert,覆写为 alert。structured_output 由所有可能降级路径保证非 None,
    保留防御性 if 检查。

    Args:
        state: 当前图状态(取 structured_output)。
        runtime: LangGraph Runtime,context 即 ExpertContextSchema
            (db_session_factory / child_user_id / report_date /
             dimension_summary / crisis_detected_today)。

    Returns:
        {"structured_output": output},供 ainvoke 最终结果回传。
    """
    output = state["structured_output"]
    if output is not None:
        ctx = runtime.context

        # 二层兜底:当日有 crisis 标记 → 覆写 overall_status 为 alert
        if ctx.crisis_detected_today and output.overall_status != DailyStatus.alert:
            output = output.model_copy(update={"overall_status": DailyStatus.alert})

        async with ctx.db_session_factory() as db:
            await write_expert_results(
                db=db,
                child_user_id=ctx.child_user_id,
                session_id=ctx.session_id,
                report_date=ctx.report_date,
                output=output,
                dimension_summary=ctx.dimension_summary,
            )
            await db.commit()

    return {"structured_output": output}


# ---------------------------------------------------------------------------
# Graph builder(无参工厂 + context_schema=ExpertContextSchema)
# ---------------------------------------------------------------------------


def build_expert_graph() -> CompiledStateGraph:
    """构建专家图,无参工厂。

    资源通过 Runtime[ExpertContextSchema] DI 注入各节点;context_schema=
    ExpertContextSchema 开启 Runtime DI,各节点函数体内从 runtime.context 取资源。

    Returns:
        编译后的 CompiledStateGraph,可直接 ainvoke(state, context=expert_ctx)。
    """
    from app.domain.expert.context_schema import ExpertContextSchema

    builder = StateGraph(ExpertGraphState, context_schema=ExpertContextSchema)

    builder.add_node("load_context", load_context)
    builder.add_node("expert_llm_call", expert_llm_call)
    builder.add_node("expert_tools", expert_tools)
    builder.add_node("write_results", write_results)

    builder.add_edge("__start__", "load_context")
    builder.add_edge("load_context", "expert_llm_call")
    # expert_llm_call → expert_tools 直连:
    # 节点契约保证 expert_tools 必收到 tool_calls(无 tool_calls 由 expert_tools 防御性兜底)
    builder.add_edge("expert_llm_call", "expert_tools")
    builder.add_conditional_edges(
        "expert_tools",
        route_after_tools,
        {"expert_llm_call": "expert_llm_call", "write_results": "write_results"},
    )
    builder.add_edge("write_results", "__end__")

    return builder.compile()  # type: ignore[reportReturnType]
