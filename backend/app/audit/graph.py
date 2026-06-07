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
from typing import TYPE_CHECKING, Annotated, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from typing_extensions import TypedDict

from app.audit.llm import build_audit_llm
from app.audit.prompts import build_audit_system_prompt
from app.domain.audit.schemas import (
    AuditDimensionScores,
    AuditOutputSchema,
)
from app.domain.audit.usecase import write_audit_results

if TYPE_CHECKING:
    from langgraph.runtime import Runtime
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.audit.context_schema import AuditContextSchema

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

    `max_iter` 因 LangGraph 路由函数 route_after_tools(state) 无法接收 runtime 参数，
    此为框架限制下的妥协，由 load_context 节点从 runtime.context.max_iter 一次性写入，
    运行期不可变。（D-patch0-7）
    """

    sid: str
    turn_number: int
    child_profile: dict | None
    session_notes_working: str
    tool_iter_count: int
    structured_output: AuditOutputSchema | None
    messages: Annotated[list[BaseMessage], add_messages]
    max_iter: int  # 路由函数妥协（D-patch0-7），load_context 从 runtime.context 写入


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
    guidance_injection: str | None = None,
    turn_summary: str = "审查超时降级",
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
            romance=0,
            values=0,
            boundaries=0,
            academic=0,
            lifestyle=0,
        ),
        crisis_detected=False,
        crisis_topic=None,
        redline_triggered=False,
        redline_detail=None,
        guidance_injection=guidance_injection,
        turn_summary=turn_summary,
    )


# ---------------------------------------------------------------------------
# Helper: _load_messages_from_pg（T12：ORM 改造，零行为漂移）
# ---------------------------------------------------------------------------

# T12 保留 M8 契约：
# - 不过滤 status（原 SQL 无 WHERE status = ...）
# - ORDER BY created_at DESC LIMIT limit + Python reversed() 输出正序
# - select(Message.role, Message.content) 返回 tuple Row
#   避免破坏调用方 for r, c in reversed(rows) 解构


async def _load_messages_from_pg(
    sid: str,
    db_session_factory: async_sessionmaker[AsyncSession],
    limit: int = 10,
) -> list[BaseMessage]:
    """从 PG 读近 N 轮 messages 并转为 LangChain BaseMessage 列表。

    T12（D-patch0-7）：ORM `select(Message.role, Message.content)` 替代原始 text-SQL。
    行为与 M8 完全一致：不过滤 status、DESC LIMIT + reversed()。

    TODO(M9): 接入 rolling_summaries 闭环后，history 可包含压缩摘要。
    """
    from sqlalchemy import select

    from app.core.enums import MessageRole
    from app.models.chat import Message

    async with db_session_factory() as db:
        stmt = (
            select(Message.role, Message.content)
            .where(Message.session_id == sid)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        rows = (await db.execute(stmt)).all()

    return [
        HumanMessage(content=c) if r == MessageRole.human else AIMessage(content=c)
        for r, c in reversed(rows)
    ]


# ---------------------------------------------------------------------------
# Node: load_context
# ---------------------------------------------------------------------------


async def load_context(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """从 PG 读近 N 轮消息 + 构造首帧 messages（system + history + session_notes）。

    T11：db_session_factory 从 runtime.context 取，替代模块级会话工厂。
    T12：_load_messages_from_pg 内部 ORM 改造，对外接口不变。
    """
    ctx = runtime.context
    history = await _load_messages_from_pg(
        str(ctx.session_id),
        ctx.db_session_factory,
    )
    return {
        "messages": [
            SystemMessage(content=build_audit_system_prompt()),
            *history,
            HumanMessage(content=f"当前 session_notes：\n{state['session_notes_working']}"),
        ],
        "max_iter": ctx.max_iter,  # D-patch0-7：路由函数妥协
    }


# ---------------------------------------------------------------------------
# Node: audit_llm_call
# ---------------------------------------------------------------------------


async def audit_llm_call(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """调审查 LLM（三 tool + tool_choice="auto"）。

    T11：build_audit_llm 从 runtime.context.settings 取参数，替代 closure 注入。
    D11 v3（M8-hotfix）：tool_choice="auto" + post-processing 兜底。
    """
    ctx = runtime.context
    llm = build_audit_llm(ctx.settings)
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
            ),
        )
        response = await llm.ainvoke(messages)

        if not response.tool_calls:
            # 两次都未调 audit_output → 降级
            # guidance_injection 不再传运营态字符串：降级时主 LLM 不应收到
            # 任何 guidance 注入（route_by_risk.ready 分支 guidance 为 None
            # → 落到 main 分支而非 guidance 分支）。
            logger.warning("audit_pipeline: 模型连续两次未调用 audit_output，默认 verdict=warn")
            return {
                "messages": [response],
                "structured_output": _build_audit_output_default(
                    turn_summary="审查降级：模型未调用 audit_output",
                ),
            }

    return {"messages": [response]}


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


async def audit_tools(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """自写 ToolNode：处理 AppendNote / ReplaceInNotes。

    T11：max_iter 从 runtime.context.max_iter 取，替代 closure 注入。
    超限时构造降级 AuditOutputSchema 并强制退出 loop。
    """
    ctx = runtime.context
    return await _audit_tools_impl(state, ctx.max_iter)


async def _audit_tools_impl(state: AuditGraphState, max_iter: int) -> dict:
    """audit_tools 内部实现，max_iter 由 audit_tools 节点传入。"""
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
                    "error": f"old_str matches {count} times, extend context to make it unique",
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
            state["sid"],
            state["turn_number"],
            iter_count,
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
# Route: route_after_tools（D-patch0-7：max_iter 从 state 读取）
# ---------------------------------------------------------------------------


def route_after_tools(state: AuditGraphState) -> Literal["audit_llm_call", "write_results"]:
    """超限强制退出 loop → write_results；否则返回 audit_llm_call。

    max_iter 从 state 读取（D-patch0-7：LangGraph 路由函数无法接收 runtime 参数的妥协）。
    """
    max_iter = state.get("max_iter", 5)
    if state["tool_iter_count"] >= max_iter:
        logger.warning(
            "audit.loop_exceeded sid=%s turn=%s iter=%d max=%d",
            state["sid"],
            state["turn_number"],
            state["tool_iter_count"],
            max_iter,
        )
        return "write_results"
    return "audit_llm_call"


# ---------------------------------------------------------------------------
# Node: write_results
# ---------------------------------------------------------------------------


async def write_results(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """从 structured_output 或 messages 末尾 AIMessage 提取结果 → 落库。

    T11：db_session_factory 从 runtime.context 取，替代模块级会话工厂。
    双引擎遗留标注（§A.2）：patch1 / M9 主体期统一。

    注意：上下文管理器仅关闭 session 不会自动提交。
    write_audit_results 写入后必须显式 commit 确保数据持久化。
    """
    output = state["structured_output"]
    if output is None:
        last_ai = _last_aimessage(state["messages"])
        if last_ai and last_ai.tool_calls:
            output = AuditOutputSchema.model_validate(last_ai.tool_calls[0]["args"])

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
    from app.audit.context_schema import AuditContextSchema

    builder = StateGraph(AuditGraphState, context_schema=AuditContextSchema)

    builder.add_node("load_context", load_context)
    builder.add_node("audit_llm_call", audit_llm_call)
    builder.add_node("audit_tools", audit_tools)
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
        route_after_tools,
        {"audit_llm_call": "audit_llm_call", "write_results": "write_results"},
    )
    builder.add_edge("write_results", END)

    return builder.compile()  # type: ignore[reportReturnType]
