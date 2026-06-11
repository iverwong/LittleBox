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

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError
from typing_extensions import TypedDict

from app.core.history_xml import serialize_history_to_xml
from app.domain.accounts.schemas import ChildProfileSnapshot
from app.domain.audit.llm import build_audit_llm
from app.domain.audit.prompts import build_audit_system_prompt
from app.domain.audit.schemas import (
    AuditDimensionScores,
    AuditOutputSchema,
)
from app.domain.audit.usecase import write_audit_results

if TYPE_CHECKING:
    from langgraph.runtime import Runtime
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.domain.audit.context_schema import AuditContextSchema

logger = logging.getLogger("audit.graph")

TOOL_NAME_APPEND = "AppendNote"
TOOL_NAME_REPLACE = "ReplaceInNotes"
TOOL_NAME_OUTPUT = "AuditOutputSchema"


def _has_audit_output(response: AIMessage) -> bool:
    """检查模型的响应中是否调用了 audit_output 工具（D11 v3 post-processing）。"""
    return any(tc["name"] == TOOL_NAME_OUTPUT for tc in (response.tool_calls or []))


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

    `max_iter` 因 LangGraph 路由函数 route_after_tools(state) 无法接收 runtime 参数，
    此为框架限制下的妥协，由 load_context 节点从 runtime.context.max_iter 一次性写入，
    运行期不可变。（D-patch0-7）
    """

    sid: str
    turn_number: int
    child_profile: ChildProfileSnapshot | None  # 暂传 None，sensitivity 接入时改真值
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
    from app.domain.chat.models import Message

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


async def _load_session_notes_from_pg(
    sid: str,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """从 PG 读该 session 的历史 `session_notes`（per-session 唯一行）。

    用于 load_context 跨轮注入：worker 层只 seed `session_notes_working=""`，
    真实历史 notes 需从 `RollingSummary` 表读，否则跨轮被静默覆盖。
    """
    from sqlalchemy import select

    from app.domain.audit.models import RollingSummary

    async with db_session_factory() as db:
        rs = await db.scalar(
            select(RollingSummary).where(RollingSummary.session_id == sid).limit(1)
        )
    if rs is None or rs.session_notes is None:
        return ""
    return rs.session_notes


# ---------------------------------------------------------------------------
# Node: load_context
# ---------------------------------------------------------------------------


async def load_context(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """从 PG 读近 N 轮消息 + 历史 session_notes + 构造首帧 messages。

    切分规则：最近 8 条（4 轮 H/A）拆成"前 3 轮（6 条）"和"当前 1 轮（2 条）"
    两段，分别用 serialize_history_to_xml 包装为单条 HumanMessage 嵌入 prompt，
    避免 chat template 把多轮 H/A 末帧视为 generation prefix 触发续写。

    session_notes 从 PG 读 + seed 到 working copy：worker.py:153 仍 seed 空串，
    但实际历史由本节点注入（避免跨轮被静默覆盖）。

    T11：db_session_factory 从 runtime.context 取，替代模块级会话工厂。
    T12：_load_messages_from_pg 内部 ORM 改造，对外接口不变。
    """
    ctx = runtime.context
    sid = str(ctx.session_id)
    history = await _load_messages_from_pg(sid, ctx.db_session_factory, limit=8)
    prior_turns = history[:-2]  # 前 3 轮 = 6 条消息
    current_turn = history[-2:]  # 当前 1 轮 = 2 条消息
    session_notes = await _load_session_notes_from_pg(sid, ctx.db_session_factory)
    prior_xml = serialize_history_to_xml(prior_turns, include_system=False)
    current_xml = serialize_history_to_xml(current_turn, include_system=False)
    return {
        "messages": [
            SystemMessage(content=build_audit_system_prompt()),
            HumanMessage(
                content=(
                    f"以下是该会话最近 3 轮历史对话：\n{prior_xml}\n\n"
                    f"以下是当前轮次对话：\n{current_xml}\n\n"
                    f"当前 session_notes：\n{session_notes}"
                )
            ),
        ],
        "session_notes_working": session_notes,
        "max_iter": ctx.max_iter,  # D-patch0-7：路由函数妥协
    }


# ---------------------------------------------------------------------------
# Node: audit_llm_call
# ---------------------------------------------------------------------------


async def audit_llm_call(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """调审查 LLM + 纯文本追问 + 恰好单 OUTPUT 时解析为 structured_output。

    解析边界：响应恰好 1 个 tool_call 且 name == TOOL_NAME_OUTPUT。
    其余情况（含 0 个、≥2 个、混调）原样返回，structured_output 不设，
    交由 audit_tools 处理（含 max_iter 尾部兜底）。

    T11：build_audit_llm 从 runtime.context.settings 取参数，替代 closure 注入。
    D11 v3（M8-hotfix）：tool_choice="auto" + post-processing 兜底。
    C2 收紧：删除 in-node 多 OUTPUT retry / 缓存 [-1] 改写，协议违规收敛到
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

    # ---- OUTPUT 解析：仅当恰好单 OUTPUT 时 ----
    result: dict[str, Any] = {"messages": [response]}
    if (
        response.tool_calls
        and len(response.tool_calls) == 1
        and response.tool_calls[0]["name"] == TOOL_NAME_OUTPUT
    ):
        result["structured_output"] = AuditOutputSchema.model_validate(
            response.tool_calls[0]["args"]
        )
    return result


# ---------------------------------------------------------------------------
# Route: route_after_llm
# ---------------------------------------------------------------------------


def route_after_llm(state: AuditGraphState) -> Literal["audit_tools", "write_results"]:
    """structured_output 非空 → write_results；否则 → audit_tools。

    write_results 分支承载三种情况：
    - 单 OUTPUT 解析：audit_llm_call 设的 structured_output
    - 双失败降级：audit_llm_call 设的 _build_audit_output_default
    - max_iter 兜底：audit_tools 设的（扫历史最后 OUTPUT 或 default）

    其余情况（带 tool_calls：note-only / 混调 / 多 OUTPUT）→ audit_tools
    让 audit_tools 节点处理（应用笔记 + 发 error ToolMessage 触发 loop 修正）。
    """
    if state.get("structured_output") is not None:
        return "write_results"
    return "audit_tools"


# ---------------------------------------------------------------------------
# Node: audit_tools
# ---------------------------------------------------------------------------


async def audit_tools(
    state: AuditGraphState,
    runtime: Runtime[AuditContextSchema],
) -> dict:
    """自写 ToolNode：处理 AppendNote / ReplaceInNotes + 回应 OUTPUT 违规。

    职责（配对永远成立：每个 tool_call 都对应一个 ToolMessage）：
    - APPEND / REPLACE：照常应用，统一在循环底部按"失败 OR 最后一个 note"规则附 current_notes
    - OUTPUT（混调 或 ≥2 个）：不解析，对每个这种 OUTPUT 发 error ToolMessage，
      "请单独调用一次 audit_output 给出最终结论，不要与笔记工具混调或重复调用"
      触发 loop 让 LLM 修正（下次应给 [NOTE] 或 [NOTE, NOTE] 或 [OUTPUT]）

    audit_llm_call 已在源头把"单独一次 OUTPUT"解析为 state.structured_output，
    路由已走 write_results 分支，本节点不再处理"干净 OUTPUT"。

    T11：max_iter 从 runtime.context 取，替代 closure 注入。
    """
    ctx = runtime.context
    max_iter = ctx.max_iter
    last_ai = _last_aimessage(state["messages"])
    # 防御性：正常流程不可达；双失败降级已在 audit_llm_call 设 structured_output 走 write_results
    # 此处兜底给一个 default structured_output，杜绝空转回环
    if last_ai is None or not last_ai.tool_calls:
        return {
            "structured_output": _build_audit_output_default(
                turn_summary="审查降级：audit_tools 无 tool_call",
            ),
        }

    tool_messages: list[ToolMessage] = []
    new_notes = state["session_notes_working"]

    note_tcs = [
        tc for tc in last_ai.tool_calls if tc["name"] in (TOOL_NAME_APPEND, TOOL_NAME_REPLACE)
    ]
    last_note_tc = note_tcs[-1] if note_tcs else None

    for tc in last_ai.tool_calls:
        name, args, tid = tc["name"], tc["args"], tc["id"]
        if name == TOOL_NAME_OUTPUT:
            # 混调 / 多 OUTPUT 违规：发 error ToolMessage，不解析
            payload = {
                "error": "请单独调用一次 audit_output 给出最终结论，不要与笔记工具混调或重复调用"
            }
            tool_messages.append(
                ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=tid),
            )
            continue
        is_last_note = tc is last_note_tc
        if name == TOOL_NAME_APPEND:
            new_notes = new_notes + ("\n" if new_notes else "") + args["text"]
            payload: dict = {"ok": True}
        elif name == TOOL_NAME_REPLACE:
            old, new = args["old_str"], args["new_str"]
            count = new_notes.count(old)
            if count == 0:
                payload = {"error": "old_str not found"}
            elif count >= 2:
                payload = {
                    "error": (f"old_str matches {count} times, extend context to make it unique")
                }
            else:
                new_notes = new_notes.replace(old, new, 1)
                payload = {"ok": True}
        else:
            payload = {"error": f"unknown tool: {name}"}
        # 统一在循环底部赋值 current_notes：失败响应（LLM 需 state 修复 old_str）
        # OR 最后一个 note 工具的成功响应
        if not payload.get("ok") or is_last_note:
            payload["current_notes"] = new_notes
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
        # 最后一条未应用 tool_call 的内容 append 到 notes
        last_tc = last_ai.tool_calls[-1]
        fallback_text = last_tc["args"].get("text") or last_tc["args"].get("new_str", "")
        if fallback_text:
            new_notes += f"\n[审查 agent 多次尝试修改未果，原始建议如下]\n{fallback_text}"
            result_dict["session_notes_working"] = new_notes
        # 扫 message 历史取最后一个 OUTPUT 的 args 构造 structured_output
        last_output_tc = _find_last_output_tool_call(state["messages"])
        if last_output_tc is not None:
            # 校验失败回落 default，避免历史 OUTPUT args 非法时抛
            try:
                structured = AuditOutputSchema.model_validate(last_output_tc["args"])
            except ValidationError as exc:
                logger.warning(
                    "audit.max_iter_salvage_validation_failed turn=%s err=%s",
                    state["turn_number"],
                    exc,
                )
                structured = _build_audit_output_default()
            else:
                # 校验通过：强制 guidance_injection=None（守 M9.5）
                # 保留 dimension_scores / crisis / redline / turn_summary
                structured.guidance_injection = None
            result_dict["structured_output"] = structured
        else:
            # 历史中无任何 OUTPUT，落回 _build_audit_output_default
            result_dict["structured_output"] = _build_audit_output_default()
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
