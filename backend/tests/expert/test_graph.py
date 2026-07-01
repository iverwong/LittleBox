"""Expert 图节点测试：4 节点 + 1 条件路由。

通过 fake LLM + mock DB 实现隔离，直接调节点函数 + 图集成测试。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core.enums import DailyStatus
from app.core.time import SHANGHAI
from app.domain.expert.context_schema import ExpertContextSchema
from app.domain.expert.graph import (
    ExpertGraphState,
    _build_degraded_output,
    _last_aimessage,
    build_expert_graph,
    expert_llm_call,
    expert_tools,
    load_context,
    route_after_tools,
    write_results,
)
from app.domain.expert.schemas import (
    DailyDimensionSummary,
    ExpertReportSchema,
    SearchSourceType,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

# 注意：pytest.mark.asyncio 只在需要 async 的类上单独标注

CUID = uuid.uuid4()
SID = uuid.uuid4()
REPORT_DATE = date(2026, 6, 23)

# ---------------------------------------------------------------------------
# FakeExpertLLM
# ---------------------------------------------------------------------------


class FakeExpertLLM:
    """预定义 AIMessage 响应序列的假 LLM。"""

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)

    async def ainvoke(self, _input, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return _aim(tool_calls=[_TC_OUTPUT])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aim(
    content: str = "",
    tool_calls: list[dict] | None = None,
    token_usage: dict | None = None,
    usage_metadata: dict | None = None,
) -> AIMessage:
    """构造 fake AIMessage。

    兼容两种用法:
    - ``token_usage``: 旧 response_metadata["token_usage"] 形式(只为后向兼容保留)
    - ``usage_metadata``: 新 form,模拟 LangChain SDK 自动设置的 usage_metadata 字段
    """
    msg = AIMessage(content=content, tool_calls=tool_calls or [])
    if token_usage is not None:
        msg.response_metadata["token_usage"] = token_usage
    if usage_metadata is not None:
        msg.usage_metadata = usage_metadata
    return msg


def _tc(name: str, args: dict, call_id: str | None = None) -> dict:
    return {"name": name, "args": args, "id": call_id or f"call-{name}"}


_TC_OUTPUT = _tc(
    "ExpertReportSchema",
    {
        "overall_status": "stable",
        "degraded": False,
        "today_overview": "平稳的一天",
        "what_was_discussed": "讨论了学校生活",
        "emotion_changes": "情绪稳定",
        "noteworthy": "无特别",
        "suggestions": "继续观察",
        "anomaly_periods": "无",
    },
)

_TC_SEARCH = _tc("SearchHistoryInput", {"keywords": ["游戏", "学校"]})
_TC_FETCH = _tc(
    "FetchByRefInput",
    {"search_source": SearchSourceType.TURN_SUMMARY.value, "ref": str(uuid.uuid4())},
)


def _make_mock_db() -> MagicMock:
    """构造 mock DB session 用于 load_context 和 write_results。"""
    mock_db = MagicMock()
    # execute returns a result mock whose fetchall returns empty list
    result_mock = MagicMock()
    result_mock.fetchall.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)
    # ``_fetch_today_materials`` 走 ``db.scalar`` 与 ``db.scalars``:
    # 都需要返回空集(None 或空迭代器)以与"无数据"路径对齐
    mock_db.scalar = AsyncMock(return_value=None)
    scalars_iter = MagicMock()
    scalars_iter.__iter__.return_value = iter([])
    mock_db.scalars = AsyncMock(return_value=scalars_iter)
    mock_db.commit = AsyncMock()
    return mock_db


def _make_mock_db_cm() -> AsyncMock:
    """构造 async context manager 返回 mock db。"""
    cm = AsyncMock()
    cm.__aenter__.return_value = _make_mock_db()
    return cm


def _make_mock_ctx(**overrides) -> ExpertContextSchema:
    """构造最小 ExpertContextSchema（mock 资源字段）。"""
    defaults = dict(
        child_user_id=CUID,
        owned_session_ids=frozenset({SID}),
        session_id=SID,
        report_date=REPORT_DATE,
        dimension_summary=DailyDimensionSummary(peak=0.0, mean=0.0, high_ratio=0.0),
        crisis_detected_today=False,
        max_output_attempts=3,
        token_budget=100_000,
        child_profile=MagicMock(),
        settings=MagicMock(),
        db_session_factory=MagicMock(return_value=_make_mock_db_cm()),
        shared_http_client=MagicMock(),
    )
    defaults.update(overrides)
    return ExpertContextSchema(**defaults)


def _make_fake_runtime(**ctx_overrides) -> SimpleNamespace:
    """构造 fake Runtime[ExpertContextSchema]。"""
    return SimpleNamespace(context=_make_mock_ctx(**ctx_overrides))


def _initial_state(**overrides) -> ExpertGraphState:
    """构造初始 ExpertGraphState。"""
    state: ExpertGraphState = {
        "messages": [],
        "output_attempts": 0,
        "total_output_tokens": 0,
        "structured_output": None,
        "_budget_forced": False,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Tests: Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    """辅助函数测试。"""

    def test_last_aimessage_finds_last(self):
        msg1 = HumanMessage(content="hello")
        msg2 = AIMessage(content="world")
        msg3 = ToolMessage(content="tool", tool_call_id="1")
        assert _last_aimessage([msg1, msg2, msg3]) is msg2

    def test_last_aimessage_none(self):
        assert _last_aimessage([HumanMessage(content="hello")]) is None
        assert _last_aimessage([]) is None

    def test_build_degraded_output_no_crisis(self):
        out = _build_degraded_output(crisis_detected_today=False)
        assert out.degraded is True
        assert out.overall_status == DailyStatus.attention

    def test_build_degraded_output_with_crisis(self):
        out = _build_degraded_output(crisis_detected_today=True)
        assert out.degraded is True
        assert out.overall_status == DailyStatus.alert


# ---------------------------------------------------------------------------
# Tests: load_context node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLoadContextNode:
    """load_context 节点测试。"""

    async def test_returns_messages_with_system_and_human(self):
        result = await load_context(
            _initial_state(),
            _make_fake_runtime(),
        )
        msgs = result["messages"]
        assert len(msgs) >= 2
        assert isinstance(msgs[0], SystemMessage)
        assert isinstance(msgs[1], HumanMessage)
        assert result["output_attempts"] == 0
        assert result["total_output_tokens"] == 0
        assert result["structured_output"] is None
        assert result["_budget_forced"] is False

    async def test_human_message_contains_report_date(self):
        result = await load_context(
            _initial_state(),
            _make_fake_runtime(),
        )
        human = result["messages"][1]
        assert isinstance(human, HumanMessage)
        assert str(REPORT_DATE) in human.content

    async def test_no_owned_sids_skips_db(self):
        """无 owned_sids 时跳过 DB 查询，HumanMessage 不含对话材料头。"""
        result = await load_context(
            _initial_state(),
            _make_fake_runtime(owned_session_ids=frozenset()),
        )
        human = result["messages"][1]
        assert isinstance(human, HumanMessage)
        assert "今日对话材料" not in human.content


# ---------------------------------------------------------------------------
# Tests: expert_llm_call node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestExpertLLMCallNode:
    """expert_llm_call 节点测试。"""

    async def test_calls_llm_and_accumulates_tokens(self):
        """LLM 调用成功应设 total_output_tokens 为当次 usage.total_tokens。"""
        runtime = _make_fake_runtime()
        fake = FakeExpertLLM(
            [
                _aim(tool_calls=[_TC_OUTPUT], usage_metadata={"total_tokens": 42}),
            ]
        )

        with patch("app.domain.expert.graph.build_expert_llm", return_value=fake):
            result = await expert_llm_call(_initial_state(), runtime)

        # messages 累加 → provider 返回的 total_tokens 已含历史 input,覆盖式即可
        assert result["total_output_tokens"] == 42
        assert len(result["messages"]) == 1
        assert result["messages"][0].tool_calls

    async def test_no_tool_calls_triggers_post_processing(self):
        """首次无 tool_calls 应触发后处理追问,追问成功则取最后一次 usage 覆盖。"""
        runtime = _make_fake_runtime()
        fake = FakeExpertLLM(
            [
                _aim(content="text reply", tool_calls=[]),
                _aim(tool_calls=[_TC_OUTPUT], usage_metadata={"total_tokens": 58}),
            ]
        )

        with patch("app.domain.expert.graph.build_expert_llm", return_value=fake):
            result = await expert_llm_call(
                _initial_state(total_output_tokens=10),
                runtime,
            )

        # 覆盖式:首次无 usage 保留 state=10,追问 usage=58 覆盖 → 最终 58
        assert result["total_output_tokens"] == 58

    async def test_double_no_tool_calls_logs_warning(self):
        """连续两次无 tool_calls 应触发降级警告。"""
        runtime = _make_fake_runtime()
        fake = FakeExpertLLM(
            [
                _aim(content="first text"),
                _aim(content="second text"),
            ]
        )

        with patch("app.domain.expert.graph.build_expert_llm", return_value=fake):
            result = await expert_llm_call(_initial_state(), runtime)

        # 两次都无 usage → tokens 保留初始 state 值
        assert result["total_output_tokens"] == 0
        assert not result["messages"][0].tool_calls


# ---------------------------------------------------------------------------
# Tests: expert_tools node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestExpertToolsNode:
    """expert_tools 节点测试。"""

    async def test_single_output_valid_terminates(self):
        """单 ExpertReportSchema 校验通过 → 设 structured_output，不发 ToolMessage。"""
        state = _initial_state(
            messages=[AIMessage(content="", tool_calls=[_TC_OUTPUT])],
        )
        result = await expert_tools(state, _make_fake_runtime())
        assert result["structured_output"] is not None
        assert result["structured_output"].overall_status == DailyStatus.stable

    async def test_single_output_invalid_returns_error(self):
        """单 ExpertReportSchema 校验失败 → 发 error ToolMessage + 递增 output_attempts。"""
        bad_tc = {
            "name": "ExpertReportSchema",
            "args": {"overall_status": "invalid"},
            "id": "call-bad",
        }
        state = _initial_state(
            messages=[AIMessage(content="", tool_calls=[bad_tc])],
        )
        result = await expert_tools(state, _make_fake_runtime())
        assert "messages" in result
        assert len(result["messages"]) == 1
        assert isinstance(result["messages"][0], ToolMessage)
        payload = json.loads(result["messages"][0].content)
        assert "validation_errors" in payload
        assert result["output_attempts"] == 1

    async def test_no_tool_calls_degradation(self):
        """无 tool_calls → 防御性兜底设 structured_output。"""
        state = _initial_state(
            messages=[AIMessage(content="直接回复文本")],
        )
        result = await expert_tools(state, _make_fake_runtime())
        assert result["structured_output"] is not None
        assert result["structured_output"].degraded is True

    async def test_mixed_tool_calls_rejects_output(self):
        """混调/多 OUTPUT → 发 error ToolMessage。"""
        state = _initial_state(
            messages=[AIMessage(content="", tool_calls=[_TC_OUTPUT, _TC_SEARCH])],
        )
        with patch(
            "app.domain.expert.graph.EXPERT_TOOL_HANDLERS",
            {
                "SearchHistoryInput": AsyncMock(
                    return_value=ToolMessage(content="[]", tool_call_id="call-1"),
                ),
            },
        ):
            result = await expert_tools(state, _make_fake_runtime())
        output_errs = [
            m
            for m in result.get("messages", [])
            if isinstance(m, ToolMessage) and "单独调用" in m.content
        ]
        assert len(output_errs) == 1
        assert "structured_output" not in result

    async def test_data_tool_only_executes_handler(self):
        """仅数据工具 → 调 handler 返回 ToolMessage。"""
        state = _initial_state(
            messages=[AIMessage(content="", tool_calls=[_TC_SEARCH])],
        )
        with patch(
            "app.domain.expert.graph.EXPERT_TOOL_HANDLERS",
            {
                "SearchHistoryInput": AsyncMock(
                    return_value=ToolMessage(content="[]", tool_call_id="call-1"),
                ),
            },
        ):
            result = await expert_tools(state, _make_fake_runtime())

        assert "messages" in result
        assert len(result["messages"]) == 1

    async def test_max_attempts_after_invalid_output(self):
        """多次无效 OUTPUT 达到上限后尾部兜底（通过 multi-path 触发 max 检查）。"""
        # 混调：invalid output + 未定义工具 → 路由到 multi-path
        bad_tc = {
            "name": "ExpertReportSchema",
            "args": {"overall_status": "invalid"},
            "id": "call-bad",
        }
        other_tc = {"name": "SomeOtherTool", "args": {}, "id": "call-other"}
        messages = [AIMessage(content="", tool_calls=[bad_tc, other_tc])]
        state = _initial_state(
            output_attempts=2,
            messages=messages,
        )
        result = await expert_tools(
            state,
            _make_fake_runtime(max_output_attempts=3),
        )
        # output_attempts = 2 + 1 = 3 >= max=3 → 降级
        assert result.get("structured_output") is not None
        assert result["structured_output"].degraded is True

    async def test_token_budget_forced_message(self):
        """token 超上限且未催缴 → 注入 HumanMessage。"""
        state = _initial_state(
            total_output_tokens=100_001,
            _budget_forced=False,
            messages=[AIMessage(content="", tool_calls=[_TC_SEARCH, _TC_FETCH])],
        )
        runtime = _make_fake_runtime(token_budget=100_000)

        with patch(
            "app.domain.expert.graph.EXPERT_TOOL_HANDLERS",
            {
                "SearchHistoryInput": AsyncMock(
                    return_value=ToolMessage(content="[]", tool_call_id="call-1"),
                ),
                "FetchByRefInput": AsyncMock(
                    return_value=ToolMessage(content="{}", tool_call_id="call-2"),
                ),
            },
        ):
            result = await expert_tools(state, runtime)

        human_msgs = [m for m in result.get("messages", []) if isinstance(m, HumanMessage)]
        assert len(human_msgs) == 1
        assert "token 预算" in human_msgs[0].content
        # 数据工具被拒绝（budget_exceeded 且 force_msg_sent 已发送）
        tool_msgs = [m for m in result.get("messages", []) if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 2  # 两个拒绝消息（search + fetch）

    async def test_undefined_tool_call(self):
        """未定义的工具 → 发 error ToolMessage。"""
        unknown = {"name": "UnknownTool", "args": {}, "id": "call-unknown"}
        state = _initial_state(
            messages=[AIMessage(content="", tool_calls=[unknown])],
        )
        result = await expert_tools(state, _make_fake_runtime())
        tool_msgs = [m for m in result.get("messages", []) if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
        assert "未定义的 tool_call" in tool_msgs[0].content

    async def test_data_tool_budget_exceeded_rejected(self):
        """预算超限时数据工具被拒绝。"""
        state = _initial_state(
            total_output_tokens=100_001,
            _budget_forced=True,
            messages=[AIMessage(content="", tool_calls=[_TC_SEARCH])],
        )
        runtime = _make_fake_runtime(token_budget=100_000)
        result = await expert_tools(state, runtime)
        tool_msgs = [m for m in result.get("messages", []) if isinstance(m, ToolMessage)]
        assert any("预算已超限" in m.content for m in tool_msgs)


# ---------------------------------------------------------------------------
# Tests: route_after_tools
# ---------------------------------------------------------------------------


class TestRouteAfterTools:
    """route_after_tools 条件路由测试。"""

    def test_structured_output_set_routes_to_write(self):
        state = _initial_state(structured_output=MagicMock(spec=ExpertReportSchema))
        assert route_after_tools(state) == "write_results"

    def test_no_structured_output_routes_to_llm(self):
        state = _initial_state()
        assert route_after_tools(state) == "expert_llm_call"

    def test_none_routes_to_llm(self):
        state = _initial_state(structured_output=None)
        assert route_after_tools(state) == "expert_llm_call"


# ---------------------------------------------------------------------------
# Tests: write_results node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWriteResultsNode:
    """write_results 节点测试。"""

    async def _run_write_results(self, output, **ctx_overrides):
        """Helper to run write_results with proper mock setup."""
        mock_db = MagicMock()
        mock_db.commit = AsyncMock()
        db_cm = AsyncMock()
        db_cm.__aenter__.return_value = mock_db
        overrides = dict(db_session_factory=MagicMock(return_value=db_cm))
        overrides.update(ctx_overrides)
        runtime = _make_fake_runtime(**overrides)
        return mock_db, runtime

    async def test_writes_output_to_db(self):
        """正常输出应调用 write_expert_results 并 commit,节点返回空 dict。"""
        output = ExpertReportSchema(
            overall_status=DailyStatus.stable,
            today_overview="平稳",
            what_was_discussed="学校",
            emotion_changes="无",
            noteworthy="无",
            suggestions="保持",
            anomaly_periods="无",
        )
        state = _initial_state(structured_output=output)
        mock_db, runtime = await self._run_write_results(output)

        with patch("app.domain.expert.graph.write_expert_results", AsyncMock()) as mock_write:
            result = await write_results(state, runtime)

        mock_write.assert_awaited_once()
        mock_db.commit.assert_awaited_once()
        # 节点不返回 state 字段,返回空 dict(与 audit graph 对齐)
        assert result == {}

    async def test_crisis_override_to_alert(self):
        """crisis_detected_today=True 时即使 LLM 输出 stable 也覆写为 alert。"""
        output = ExpertReportSchema(
            overall_status=DailyStatus.stable,
            today_overview="平稳",
            what_was_discussed="学校",
            emotion_changes="无",
            noteworthy="无",
            suggestions="保持",
            anomaly_periods="无",
        )
        state = _initial_state(structured_output=output)
        mock_db, runtime = await self._run_write_results(
            output,
            crisis_detected_today=True,
        )

        with patch("app.domain.expert.graph.write_expert_results", AsyncMock()) as mock_write:
            await write_results(state, runtime)

        mock_write.assert_awaited_once()
        written_output = mock_write.call_args[1]["output"]
        assert written_output.overall_status == DailyStatus.alert

    async def test_none_output_does_not_write(self):
        """structured_output 为 None 时不落库,但节点仍返回空 dict。"""
        state = _initial_state()
        runtime = _make_fake_runtime()

        with patch("app.domain.expert.graph.write_expert_results", AsyncMock()) as mock_write:
            result = await write_results(state, runtime)

        mock_write.assert_not_awaited()
        assert result == {}


# ---------------------------------------------------------------------------
# Tests: Graph integration（无参工厂 + 图结构）
# ---------------------------------------------------------------------------


class TestGraphFactory:
    """build_expert_graph 无参工厂 + 图结构测试。"""

    def test_factory_returns_compiled_graph(self):
        graph = build_expert_graph()
        assert graph is not None
        assert hasattr(graph, "ainvoke")

    def test_graph_has_four_nodes_check_names(self):
        """检查图的节点名称列表包含预期 4 个节点。"""
        graph = build_expert_graph()
        node_names = list(graph.nodes.keys())
        assert "load_context" in node_names
        assert "expert_llm_call" in node_names
        assert "expert_tools" in node_names
        assert "write_results" in node_names


@pytest.mark.asyncio
class TestGraphIntegration:
    """专家图端到端集成测试。"""

    async def test_happy_path_returns_structured_output(self, monkeypatch):
        """Happy path：load_context → llm → tools(OUTPUT) → write_results。"""
        fake = FakeExpertLLM(
            [
                _aim(tool_calls=[_TC_OUTPUT], usage_metadata={"total_tokens": 50}),
            ]
        )
        monkeypatch.setattr("app.domain.expert.graph.build_expert_llm", lambda s, **kw: fake)

        async def _mock_write(**kw):
            pass

        monkeypatch.setattr("app.domain.expert.graph.write_expert_results", _mock_write)

        graph = build_expert_graph()
        ctx = _make_mock_ctx()

        result = await graph.ainvoke(
            _initial_state(),
            context=ctx,
        )

        assert result.get("structured_output") is not None
        assert result["structured_output"].overall_status == DailyStatus.stable
        assert result["total_output_tokens"] == 50

    async def test_no_tool_calls_degradation_path(self, monkeypatch):
        """无 tool_calls → 降级路径。"""
        fake = FakeExpertLLM(
            [
                _aim(content="纯文本回复"),
                _aim(content="也还是纯文本回复"),
            ]
        )
        monkeypatch.setattr("app.domain.expert.graph.build_expert_llm", lambda s, **kw: fake)

        async def _mock_write(**kw):
            pass

        monkeypatch.setattr("app.domain.expert.graph.write_expert_results", _mock_write)

        graph = build_expert_graph()
        ctx = _make_mock_ctx()

        result = await graph.ainvoke(
            _initial_state(),
            context=ctx,
        )

        assert result.get("structured_output") is not None
        assert result["structured_output"].degraded is True

    async def test_data_tool_route(self, monkeypatch):
        """仅数据工具 → 返回 ToolMessage → LLM 再调用 → 最终输出。"""
        fake = FakeExpertLLM(
            [
                _aim(tool_calls=[_TC_SEARCH], usage_metadata={"total_tokens": 30}),
                _aim(tool_calls=[_TC_OUTPUT], usage_metadata={"total_tokens": 70}),
            ]
        )
        monkeypatch.setattr("app.domain.expert.graph.build_expert_llm", lambda s, **kw: fake)

        async def _mock_write(**kw):
            pass

        monkeypatch.setattr("app.domain.expert.graph.write_expert_results", _mock_write)

        mock_handler = AsyncMock(
            return_value=ToolMessage(
                content=json.dumps({"results": [], "total": 0}),
                tool_call_id="call-1",
            ),
        )
        monkeypatch.setattr(
            "app.domain.expert.graph.EXPERT_TOOL_HANDLERS",
            {"SearchHistoryInput": mock_handler},
        )

        graph = build_expert_graph()
        ctx = _make_mock_ctx()

        result = await graph.ainvoke(
            _initial_state(),
            context=ctx,
        )

        assert result.get("structured_output") is not None
        # 覆盖式:最后一次 llm_call 的 usage.total_tokens=70 覆盖之前的 30
        assert result["total_output_tokens"] == 70
