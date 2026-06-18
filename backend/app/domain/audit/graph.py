"""审查 LangGraph agentic loop（M8 Step 5 / M8-hotfix Step 4 / T11+T12）。

三阶段（load_context → audit_llm_call ↔ tool loop → write_results）：

    START → load_context → audit_llm_call → route_after_llm
                             ├─ "audit_tools" → audit_tools → route_after_tools
                             │   ├─ "audit_llm_call" → audit_llm_call
                             │   └─ "write_results" → write_results → END
                             └─ "write_results" → write_results → END

D11 v3（M8-hotfix）：tool_choice="auto" + system prompt 强约束 + post-processing 兜底。
DS/BL 两端在思考模式下均不支持 tool_choice="required" 或 "any"（36 变体穷尽实证），
走 auto 是覆盖主备两端的唯一可行写法。post-processing 在 LLM 未调 audit_output
时发起一次追问，仍不调则降级 verdict=warn。

T11（D-patch0-7）：无参工厂 + 4 节点 Runtime[AuditContextSchema] DI 替代 closure 注入。
T12：_load_messages_from_pg text-SQL → ORM `select(Message.role, Message.content)`，
保留 M8 契约（不过滤 status、DESC + LIMIT + Python reversed()）。
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
    AppendNote,
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

TOOL_NAME_APPEND = "AppendNote"
TOOL_NAME_REPLACE = "ReplaceInNotes"
TOOL_NAME_OUTPUT = "AuditOutputSchema"


def _find_last_output_tool_call(messages: list[BaseMessage]) -> Any:
    """从 messages 历史反向查找最近的 AuditOutputSchema tool_call。

    用于 max_iter 兜底：从历史中拿 LLM 给过的最后一份 OUTPUT args 构造
    structured_output（强制清掉 guidance_injection 守 M9.5）。
    """
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc["name"] == TOOL_NAME_OUTPUT:
                    return tc
    return None


class AuditGraphState(TypedDict):
    """审查图状态。

    `messages` 承载 LLM ↔ tool 循环累积，通过 ``add_messages`` reducer 自动追加。
    `load_context` 节点构造首帧 messages（含 system prompt + 历史对话 + 当前 session_notes）。
    tool loop 中 audit_tools 返回的 ToolMessage 通过此 reducer 追加到 messages 末尾。
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
    """反向搜索最近的 AIMessage。"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _build_audit_output_default(
    turn_summary: str,
    guidance_injection: str | None = None,
) -> AuditOutputSchema:
    """构造降级用的默认 AuditOutputSchema（循环超限 / post-processing 兜底）。

    guidance_injection 默认 None：降级路径不向主 LLM 注入引导串，
    对齐 M9.5「正常/无结论轮必须留空」契约，避免降级串被 route_by_risk 的
    guidance 分支误投到孩子侧 prompt（intervention_type="guided"）。
    turn_summary 默认保留作诊断信息载体，会落 audit_records.turn_summary。
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
    """从 PG 读该 session 的历史 `session_notes`（per-session 唯一行）。

    用于 load_context 跨轮注入：worker 层只 seed `session_notes_working=""`，
    真实历史 notes 需从 `RollingSummary` 表读，否则跨轮被静默覆盖。
    """
    from sqlalchemy import select

    from app.domain.audit.models import RollingSummary

    rs = await db.scalar(
        select(RollingSummary.session_notes).where(RollingSummary.session_id == sid).limit(1)
    )
    if rs is None:
        return """## 话题脉络
（按时间记录聊了哪些话题/事件）

## 风险观察
（记录是哪个维度、什么苗头、趋势如何，可带入风控判断）

## 情绪走向
（整段叙事描述情绪如何变化）

## 发展性话题观察
（正常发展话题：中性记录孩子在探索什么，不做风险判断）

## 家长关注点回应
（对照家长填写的关注点，本会话有无相关进展）

## 待续关注
（需要下轮或跨会话继续盯的点）

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

    切分规则：最近 8 条（4 轮 H/A：from_turn=N-3 到 to_turn=N）拆成
    "前 3 轮（6 条）"和"当前 1 轮（2 条）"两段，分别用 serialize_history_to_xml
    包装为单条 HumanMessage 嵌入 prompt，避免 chat template 把多轮 H/A 末帧
    视为 generation prefix 触发续写。

    session_notes 从 PG 读 + seed 到 working copy：worker.py:153 仍 seed 空串，
    但实际历史由本节点注入（避免跨轮被静默覆盖）。

    T11：db_session_factory 从 runtime.context 取，替代模块级会话工厂。
    T12：_load_messages_from_pg 内部 ORM 改造，对外接口不变。
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
                    f"以下是该会话最近 3 轮历史对话：\n{prior_xml}\n\n"
                    f"以下是当前轮次对话：\n{current_xml}\n\n"
                    f"当前 session_notes：\n<session_notes>{session_notes}</session_notes>"
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

    责任：
    1. 调 LLM,等回复
    2. 若回复无 tool_calls,追问一轮(post-processing),仍无则 logger.warning
       记诊断(双失败降级 structured_output 由 audit_tools 防御性兜底设 default)
    3. 有 tool_calls 的回复一律透传,structured_output 不设 → audit_tools
       由 audit_tools 做规则校验(混/多 OUTPUT)、参数校验(pydantic)、单 OUTPUT 终止

    节点契约:本节点出参恒为 {"messages": [response]},不设 structured_output。
    审计图 audit_llm_call → audit_tools 直连,无 route_after_llm 中转。

    T11:build_audit_llm 从 runtime.context.settings 取参数,替代 closure 注入。
    D11 v3(M8-hotfix):tool_choice="auto" + post-processing 兜底。
    C2 收紧:删除 in-node 多 OUTPUT retry / 缓存 [-1] 改写,协议违规收敛到
    audit_tools 发 error ToolMessage 触发 loop 修正。
    """
    ctx = runtime.context
    llm = build_audit_llm(ctx.settings)
    messages = list(state["messages"])
    response = await llm.ainvoke(messages)

    # ---- 协议违规 1：模型返回纯文本 → post-processing ----
    if not response.tool_calls:
        messages.append(response)
        messages.append(
            HumanMessage(
                content="请调用 AuditOutputSchema 工具给出最终结论"
                "（verdict 为 pass / warn / fail），"
                "不要直接回复文本。你仍然可以在调用 AuditOutputSchema"
                " 之前先调用 AppendNote 或 ReplaceInNotes"
                " 记录笔记。",
            ),
        )
        response = await llm.ainvoke(messages)
        if not response.tool_calls:
            # 两次都未调 audit_output → 降级
            # guidance_injection 不再传运营态字符串：降级时主 LLM 不应收到
            # 任何 guidance 注入（route_by_risk.ready 分支 guidance 为 None
            # → 落到 main 分支而非 guidance 分支）。
            # structured_output 由 audit_tools 防御性兜底(无 tool_call 路径)设 default。
            logger.warning("audit_pipeline: 模型连续两次未调用 audit_output，默认 verdict=warn")

    # 规则校验（混/多 OUTPUT）、参数校验（pydantic）、单 OUTPUT 终止由 audit_tools 负责
    # 本节点只把 AIMessage 透传，structured_output 不设
    return {"messages": [response]}


# ---------------------------------------------------------------------------
# Node: audit_tools
# ---------------------------------------------------------------------------


async def audit_tools(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """自写 ToolNode：规则校验 + 参数校验 + 笔记应用 + 单 OUTPUT 终止。

    职责（按 frame 分类）：
    - 整帧单 OUTPUT（且 args 校验通过）：终止信号。设 structured_output
      （M9.5 清掉 guidance_injection），不发 ToolMessage。路由到 write_results。
    - 整帧单 OUTPUT（args 校验失败）：发 error ToolMessage（带字段级
      validation_errors），不设 structured_output。路由到 audit_llm_call 修正。
    - 混调 [NOTE, OUTPUT] / 多 OUTPUT：对每个这种 OUTPUT 发 error ToolMessage
      （"请单独调用一次 audit_output..."），应用 note 工具，不设 structured_output。
      路由到 audit_llm_call 修正。
    - 仅 note 工具：应用，返回 ok ToolMessage。不设 structured_output。
      路由到 audit_llm_call 让 LLM 继续。
    - 无 tool_calls：防御性兜底，设 default structured_output。路由到 write_results。
    - max_iter 超限：扫历史最后 OUTPUT args 构造 structured_output，校验失败落 default。

    audit_llm_call 出参恒为 {"messages": [response]}（无 structured_output），
    本节点承担所有校验 + 终止决策。

    T11：max_iter 从 runtime.context 取，替代 closure 注入。
    """
    ctx = runtime.context
    max_iter = ctx.max_iter
    last_ai = _last_aimessage(state["messages"])
    # 防御性：audit_llm_call 一般情况下 last_ai.tool_calls 非空，如为空则路由到兜底策略
    if last_ai is None or not last_ai.tool_calls:
        return {
            "structured_output": _build_audit_output_default(
                turn_summary="无该轮摘要（审查降级：无工具调用）",
            ),
        }

    new_notes = state["session_notes_working"]
    payload: dict[str, Any] = {}

    # ---- 单 OUTPUT 终止路径：整帧恰好 1 个 audit_output tool_call ----
    if len(last_ai.tool_calls) == 1 and last_ai.tool_calls[0]["name"] == TOOL_NAME_OUTPUT:
        tc = last_ai.tool_calls[0]
        tid = tc["id"]
        try:
            structured = AuditOutputSchema.model_validate(tc["args"])
        except ValidationError as exc:
            # 单 OUTPUT 但 args 非法：发 error ToolMessage（带字段级 validation_errors）
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
            # 单 OUTPUT 校验通过：
            # 不发 ToolMessage —— audit_tools 出参 {"structured_output": structured}
            # 路由到 write_results；不增 tool_iter_count（终止信号，loop 结束）。
            return {"structured_output": structured}

    # ---- 多 tool_call 路径：混调 / 多 OUTPUT / 纯 note ----
    tool_messages: list[ToolMessage] = []

    note_tcs = [
        tc for tc in last_ai.tool_calls if tc["name"] in (TOOL_NAME_APPEND, TOOL_NAME_REPLACE)
    ]

    output_tcs = [tc for tc in last_ai.tool_calls if tc["name"] == TOOL_NAME_OUTPUT]

    last_note_tc = note_tcs[-1] if note_tcs else None

    if output_tcs:
        for tc in output_tcs:
            name, args, tid = tc["name"], tc["args"], tc["id"]
            # 混调 / 多 OUTPUT 违规：发 error ToolMessage，不解析
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
        if name == TOOL_NAME_APPEND:
            try:
                structured = AppendNote.model_validate(args)
            except ValidationError as exc:
                payload = {
                    "error": "AppendNote args 校验失败，请按 schema 重发",
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
                new_notes = new_notes + ("\n" if new_notes else "") + args["text"]
                payload = {"ok": True}
                if is_last_note:
                    payload["current_notes"] = new_notes
                tool_messages.append(
                    ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=tid),
                )
        elif name == TOOL_NAME_REPLACE:
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

    # max_iter 降级：尾部兜底
    if iter_count >= max_iter:
        logger.warning(
            "audit.loop_exceeded sid=%s turn=%s count=%d",
            state["sid"],
            state["turn_number"],
            iter_count,
        )

        result_dict["structured_output"] = _build_audit_output_default(
            turn_summary="无该轮摘要（审查降级：已超过迭代次数）"
        )
    return result_dict


# ---------------------------------------------------------------------------
# Route: route_after_tools（D-patch0-7：max_iter 从 state 读取）
# ---------------------------------------------------------------------------


def route_after_tools(state: AuditGraphState) -> Literal["audit_llm_call", "write_results"]:
    """structured_output 非空 或 超限 → write_results；否则 → audit_llm_call。

    structured_output 短路：若 audit_tools 已在 max_iter 兜底设了 structured_output，
    即使 iter_count 未到 max_iter，也直接 write_results。
    避免 [NOTE, OUTPUT] 路径下 LLM 持续违规时无意义 loop（修复回环+400 的关键）。
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

    无 [0] 兜底：state.structured_output 由 audit_llm_call（单 OUTPUT 解析 /
    双失败降级）或 audit_tools（max_iter 尾部兜底）保证非 None。
    保留 if output is not None 防御，对 None 状态容错（理论上不可达但保留）。

    T11：db_session_factory 从 runtime.context 取，替代模块级会话工厂。

    注意：上下文管理器仅关闭 session 不会自动提交。
    write_audit_results 写入后必须显式 commit 确保数据持久化。
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
# Graph builder（T11：无参工厂 + context_schema=AuditContextSchema）
# ---------------------------------------------------------------------------


def build_audit_graph() -> CompiledStateGraph:
    """构建审查图，无参工厂。资源通过 Runtime[AuditContextSchema] DI 注入各节点。

    T11 改无参（D-patch0-7）：删除 max_iter / settings 参数，
    context_schema=AuditContextSchema 开启 Runtime DI，各节点函数体内
    从 runtime.context 取资源，替代 M8 期 closure 注入 + 模块级会话工厂。
    """
    from app.domain.audit.context_schema import AuditContextSchema

    builder = StateGraph(AuditGraphState, context_schema=AuditContextSchema)

    builder.add_node("load_context", load_context)
    builder.add_node("audit_llm_call", audit_llm_call)
    builder.add_node("audit_tools", audit_tools)
    builder.add_node("write_results", write_results)

    builder.add_edge("__start__", "load_context")
    builder.add_edge("load_context", "audit_llm_call")
    # audit_llm_call → audit_tools 直连：
    # 节点契约保证 audit_tools 必收到 tool_calls(无 tool_calls 由 audit_tools 防御性兜底)
    builder.add_edge("audit_llm_call", "audit_tools")
    builder.add_conditional_edges(
        "audit_tools",
        route_after_tools,
        {"audit_llm_call": "audit_llm_call", "write_results": "write_results"},
    )
    builder.add_edge("write_results", "__end__")

    return builder.compile()  # type: ignore[reportReturnType]
