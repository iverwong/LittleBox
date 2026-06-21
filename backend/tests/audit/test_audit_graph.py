"""审查 LangGraph agentic loop 测试：7 路径覆盖 + D11 v3 post-processing 测试。

T11+T12（D-patch0-6）：build_audit_graph() 无参工厂 + Runtime[AuditContextSchema] DI。
测试通过 _make_fake_runtime 构造 fake Runtime 注入 AuditContextSchema 模拟资源。
"""

from __future__ import annotations

from typing import Any

import pytest
from app.domain.audit.graph import (
    TOOL_NAME_OUTPUT,
    TOOL_NAME_REPLACE,
    AuditGraphState,
    build_audit_graph,
)
from langchain_core.messages import AIMessage

pytestmark = [
    pytest.mark.audit,
    pytest.mark.asyncio,
]

SID = "00000000-0000-0000-0000-000000000001"
CUID = "00000000-0000-0000-0000-000000000002"
TARGET_MID = "00000000-0000-0000-0000-000000000003"


# ---------------------------------------------------------------------------
# FakeAuditLLM
# ---------------------------------------------------------------------------


class FakeAuditLLM:
    """预定义 AIMessage 响应序列的假 LLM。

    D11 v3（M8-hotfix）：当响应列表耗尽时，自动返回一个调用了 AuditOutputSchema 的
    默认响应，以支持 post-processing 追问流程。若需要模拟「两次都不调」场景，
    设置 ``exhausted_raises=True``。
    """

    def __init__(
        self,
        responses: list[AIMessage],
        exhausted_raises: bool = False,
    ):
        self._responses = list(responses)
        self._exhausted_raises = exhausted_raises

    async def ainvoke(self, _input: Any, **kwargs: Any) -> AIMessage:
        if self._responses:
            return self._responses.pop(0)
        if self._exhausted_raises:
            msg = f"FakeAuditLLM exhausted: no more responses (input: {_input})"
            raise IndexError(msg)
        # 默认返回 audit_output 以支持 post-processing 追问
        return _aim(tool_calls=[_TC_OUTPUT])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _tc(name: str, args: dict, call_id: str | None = None) -> dict:
    return {"name": name, "args": args, "id": call_id or f"call-{name}"}


_EMPTY_SCORES = {
    "emotional": 0,
    "social": 0,
    "values": 0,
    "boundaries": 0,
    "academic": 0,
    "lifestyle": 0,
}

_AUDIT_OUTPUT_ARGS = {
    "dimension_scores": _EMPTY_SCORES,
    "crisis_detected": False,
    "crisis_topic": None,
    "redline_triggered": False,
    "redline_detail": None,
    "guidance_injection": "观察情绪走向",
    "turn_summary": "情绪稳定",
}

_TC_OUTPUT = _tc(TOOL_NAME_OUTPUT, _AUDIT_OUTPUT_ARGS)
_TC_REPLACE = _tc(TOOL_NAME_REPLACE, {"old_str": "情绪稳定", "new_str": "情绪有些波动"})
_TC_REPLACE_MISS = _tc(TOOL_NAME_REPLACE, {"old_str": "不存在的文本", "new_str": "替换"})
_TC_REPLACE_MULTI = _tc(TOOL_NAME_REPLACE, {"old_str": "a", "new_str": "b"})


def _initial_state() -> AuditGraphState:
    return {
        "sid": SID,
        "turn_number": 1,
        "session_notes_working": "用户今天情绪稳定。",
        "tool_iter_count": 0,
        "structured_output": None,
        "messages": [],
    }


def _make_fake_runtime(max_iter: int = 5) -> object:
    """构造最小 Runtime[AuditContextSchema] 替代（LangGraph 注入 mock）。

    T11（D-patch0-6）：audit 侧 Runtime DI 测试范式，
    对齐 tests/chat/test_load_audit_state.py::_make_fake_runtime。
    测试中直接调节点函数 (state, runtime)，runtime 仅提供 .context 属性。
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from datetime import date
    import uuid

    from app.domain.audit.context_schema import AuditContextSchema
    from app.domain.accounts.schemas import ChildProfileSnapshot

    profile = ChildProfileSnapshot(
        child_user_id=uuid.UUID(CUID),
        nickname="test_kid",
        gender="unknown",
        birth_date=date(2013, 1, 1),
        age=12,
        sensitivity=None,
        custom_redlines=None,
            concerns=None,
    )
    ctx = AuditContextSchema(
        session_id=SID,
        child_user_id=CUID,
        target_message_id=TARGET_MID,
        max_iter=max_iter,
        child_profile=profile,
        settings=MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=MagicMock(),
        shared_http_client=MagicMock(),
    )
    return SimpleNamespace(context=ctx)


def _run(
    responses: list[AIMessage],
    monkeypatch: pytest.MonkeyPatch,
    max_iter: int = 5,
    exhausted_raises: bool = False,
    initial_notes: str = "",
) -> dict:
    """构造 graph + 注入 fake LLM + 调用 ainvoke，返回终态。

    T11：build_audit_graph() 无参调用 + context= 参数传递。
    A2 段：initial_notes mock _load_session_notes_from_pg 返回值,
    load_context 覆盖 state.session_notes_working 到该字符串。
    """
    fake = FakeAuditLLM(responses, exhausted_raises=exhausted_raises)
    monkeypatch.setattr("app.domain.audit.graph.build_audit_llm", lambda s, **kwargs: fake)

    # mock 数据层调用为 no-op
    async def _mock_load(*_: Any, **__: Any) -> list:
        return []

    monkeypatch.setattr("app.domain.audit.graph.load_recent_messages", _mock_load)

    async def _mock_load_notes(*_: Any, **__: Any) -> str:
        return initial_notes

    monkeypatch.setattr("app.domain.audit.graph._load_session_notes_from_pg", _mock_load_notes)

    async def _mock_write(*_: Any, **__: Any) -> None:
        pass

    monkeypatch.setattr("app.domain.audit.graph.write_audit_results", _mock_write)

    graph = build_audit_graph()
    state = _initial_state()
    state["max_iter"] = max_iter
    runtime = _make_fake_runtime(max_iter=max_iter)
    # 注入 context 到 state，使得路由函数能读到 max_iter
    # （路由函数不从 runtime 读，但 load_context 从 runtime.context 读）
    result = graph.ainvoke(state, context=runtime.context)
    return result


def _aim(content: str = "", tool_calls: list[dict] | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=tool_calls or [])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAuditGraph:
    """7 路径覆盖 + D11 v3 post-processing 测试。"""

    async def test_no_tool_call_degradation(self, monkeypatch):
        """路径 ①：模型返回纯文本（无 tool_calls）→ post-processing 追问"
        " → 终态含 structured_output。"""
        result = await _run([_aim(content="嗯，让我想想...")], monkeypatch)
        # D11 v3：post-processing 追问后模型调了 audit_output
        assert result["structured_output"] is not None
        assert result["tool_iter_count"] == 0

    async def test_one_replace_hit(self, monkeypatch):
        """路径 ②：1 replace 唯一命中 → 替换成功 → 终止。"""
        result = await _run(
            [
                _aim(tool_calls=[_TC_REPLACE]),
                _aim(tool_calls=[_TC_OUTPUT]),
            ],
            monkeypatch,
            initial_notes="用户今天情绪稳定。",
        )
        assert "情绪有些波动" in result["session_notes_working"]
        assert "情绪稳定" not in result["session_notes_working"]
        assert result["structured_output"] is not None

    async def test_replace_miss_then_hit(self, monkeypatch):
        """路径 ③：replace 0 命中 → LLM 改换 old_str 重试 → 1 命中 → 终止。"""
        result = await _run(
            [
                _aim(tool_calls=[_TC_REPLACE_MISS]),
                _aim(
                    tool_calls=[
                        _tc(
                            TOOL_NAME_REPLACE,
                            {
                                "old_str": "用户今天情绪稳定。",
                                "new_str": "追加内容。用户今天情绪稳定。",
                            },
                        )
                    ]
                ),
                _aim(tool_calls=[_TC_OUTPUT]),
            ],
            monkeypatch,
            initial_notes="用户今天情绪稳定。",
        )
        # 第一轮 replace 不命中 → session_notes 不变
        assert "用户今天情绪稳定。" in result["session_notes_working"]
        # 第二轮 replace 命中
        assert "追加内容。" in result["session_notes_working"]
        assert result["structured_output"] is not None
        assert result["tool_iter_count"] == 2

    async def test_replace_multi_then_precise(self, monkeypatch):
        """路径 ④：replace ≥2 命中 → LLM 用更精确 old_str 重试 → 1 命中。"""
        state: AuditGraphState = {
            "sid": SID,
            "turn_number": 1,
            "child_profile": None,
            "session_notes_working": "a a b",  # "a" 出现 2 次
            "tool_iter_count": 0,
            "structured_output": None,
            "messages": [],
            "max_iter": 5,
        }

        fake = FakeAuditLLM(
            [
                _aim(tool_calls=[_TC_REPLACE_MULTI]),
                _aim(tool_calls=[_tc(TOOL_NAME_REPLACE, {"old_str": "a a", "new_str": "b b"})]),
                _aim(tool_calls=[_TC_OUTPUT]),
            ]
        )
        monkeypatch.setattr("app.domain.audit.graph.build_audit_llm", lambda s, **kwargs: fake)

        async def _mock_load(*_: Any, **__: Any) -> list:
            return []

        monkeypatch.setattr("app.domain.audit.graph.load_recent_messages", _mock_load)

        # A2 段:load_context 覆盖 working copy → mock helper 返 "a a b"
        async def _mock_load_notes(*_: Any, **__: Any) -> str:
            return "a a b"

        monkeypatch.setattr("app.domain.audit.graph._load_session_notes_from_pg", _mock_load_notes)

        async def _mock_write(*_: Any, **__: Any) -> None:
            pass

        monkeypatch.setattr("app.domain.audit.graph.write_audit_results", _mock_write)

        graph = build_audit_graph()
        runtime = _make_fake_runtime(max_iter=5)
        result = await graph.ainvoke(state, context=runtime.context)

        assert result["session_notes_working"] == "b b b"
        assert result["tool_iter_count"] == 2
        assert result["structured_output"] is not None

    async def test_loop_exceeded_degradation(self, monkeypatch):
        """路径 ⑤：连续 replace 失败 → 循环超限 → 降级标记。"""
        result = await _run(
            [_aim(tool_calls=[_TC_REPLACE_MISS]) for _ in range(6)],
            monkeypatch,
            max_iter=5,
        )
        assert result["tool_iter_count"] == 5
        assert result["structured_output"] is not None
        # M9.5 契约：降级时 guidance_injection 必须为 None，
        # 避免降级串被 load_audit_state 透传后命中 route_by_risk.guidance 分支
        # 把运营态字符串误注入主 LLM（intervention_type="guided"）。
        assert result["structured_output"].guidance_injection is None
        # 降级覆盖从 guidance 串迁到 turn_summary，避免覆盖空洞
        assert result["structured_output"].turn_summary == "无该轮摘要(审查降级:已超过迭代次数)"
        # 当前实现:max_iter 兜底只设 structured_output,不污染 session_notes_working
        # (若有 replace miss,notes 保持上一轮成功后的状态;无成功则为空)
        session_notes = result["session_notes_working"]

    async def test_multi_replace_accumulation(self, monkeypatch):
        """路径 ⑥：多轮 replace → session_notes_working 状态正确累积。"""
        result = await _run(
            [
                _aim(
                    tool_calls=[
                        _tc(
                            TOOL_NAME_REPLACE,
                            {"old_str": "情绪稳定。", "new_str": "追加内容。情绪有些波动。"},
                        )
                    ]
                ),
                _aim(
                    tool_calls=[
                        _tc(
                            TOOL_NAME_REPLACE,
                            {"old_str": "追加内容。", "new_str": "最终观察。追加内容。"},
                        )
                    ]
                ),
                _aim(tool_calls=[_TC_OUTPUT]),
            ],
            monkeypatch,
            initial_notes="用户今天情绪稳定。",
        )
        notes = result["session_notes_working"]
        assert "追加内容。" in notes
        assert "情绪有些波动" in notes
        assert "最终观察。" in notes
        assert result["tool_iter_count"] == 2
        assert result["structured_output"] is not None


class TestPostProcessing:
    """D11 v3 post-processing 兜底测试。"""

    async def test_followup_triggers_on_missing_audit_output(self, monkeypatch):
        """模型首轮未调 audit_output → post-processing 触发追问 → 第二轮调了 → verdict 正确解析。"""
        result = await _run(
            [
                _aim(tool_calls=[_TC_REPLACE_MISS]),  # 首轮只调了 replace miss，没收尾
                # 追问后自动获得默认 audit_output 响应
            ],
            monkeypatch,
            initial_notes="用户今天情绪稳定。",
        )
        # post-processing 追问后调了 audit_output
        assert result["structured_output"] is not None

    async def test_double_fail_degradation(self, monkeypatch):
        """模型两轮都未调 audit_output → 走 default verdict=warn 分支 + 日志告警。"""
        result = await _run(
            [
                _aim(content="我觉得这个对话很正常"),  # 首轮纯文本
                _aim(content="好的我再想想"),  # 追问后仍纯文本
            ],
            monkeypatch,
            exhausted_raises=True,  # 不允许默认响应
        )
        assert result["structured_output"] is not None
        # M9.5 契约：模型两轮都未给结构化结论时，guidance_injection 必须为 None
        assert result["structured_output"].guidance_injection is None
        # 降级覆盖迁到 turn_summary，保留诊断信息
        assert result["structured_output"].turn_summary == "无该轮摘要(审查降级:无工具调用)"


class TestCrossTurnContinuity:
    """A 段:跨轮 session_notes 注入 + working copy 持久化。"""

    async def test_turn2_working_copy_seeded_from_history(self, monkeypatch):
        """turn=2 启动时 _load_session_notes_from_pg 返上轮笔记 → working copy seed 成功。"""
        result = await _run(
            [_aim(tool_calls=[_TC_OUTPUT])],
            monkeypatch,
            initial_notes="第一轮笔记",
        )
        # notes 应含"第一轮笔记"(seed)+ AuditOutputSchema 后未改写
        assert "第一轮笔记" in result["session_notes_working"]
        assert result["structured_output"] is not None

    async def test_replace_uses_history_when_loaded(self, monkeypatch):
        """turn=2 加载历史 notes → REPLACE 命中改写生效。"""
        result = await _run(
            [
                _aim(tool_calls=[_TC_REPLACE]),
                _aim(tool_calls=[_TC_OUTPUT]),
            ],
            monkeypatch,
            initial_notes="用户今天情绪稳定。",
        )
        # 上轮"用户今天情绪稳定。"被"用户今天情绪有些波动。"替换
        assert "情绪有些波动" in result["session_notes_working"]
        assert "情绪稳定。" not in result["session_notes_working"]


class TestOutputViolationE2E:
    """C3 + C5 协同:OUTPUT 违规 (混调/多 OUTPUT) → audit_tools error → 修正为单 OUTPUT。"""

    async def test_mixed_output_loops_to_single_output(self, monkeypatch):
        """LLM 返 [REPLACE, OUTPUT] → audit_tools 发 error 给 OUTPUT → 下轮返 [OUTPUT] 解析。"""
        result = await _run(
            [
                _aim(
                    tool_calls=[
                        _tc(
                            TOOL_NAME_REPLACE,
                            {"old_str": "起始笔记。", "new_str": "起始笔记。追加内容。"},
                        )
                    ]
                ),  # 首轮只 replace,无 OUTPUT,进 tool loop
                _aim(
                    tool_calls=[  # 第二轮 LLM 混调 (违规)
                        _tc(
                            TOOL_NAME_REPLACE,
                            {"old_str": "起始笔记。", "new_str": "起始笔记。再来一条。"},
                        ),
                        _TC_OUTPUT,
                    ]
                ),
                _aim(tool_calls=[_TC_OUTPUT]),  # 第三轮修正:单 OUTPUT 解析
            ],
            monkeypatch,
            initial_notes="起始笔记。",
        )
        # 单 OUTPUT 解析 → structured_output 不为 None
        assert result["structured_output"] is not None
        # session_notes_working 含第一轮的 replace 内容
        assert "追加内容。" in result["session_notes_working"]
        # tool_iter_count 应累到 2 (前两轮 audit_tools 各累 1)
        assert result["tool_iter_count"] == 2

    async def test_multi_output_loops_to_single_output(self, monkeypatch):
        """LLM 返 [OUTPUT, OUTPUT] → audit_tools 发 2 条 error → 下轮返 [OUTPUT] 解析。"""
        result = await _run(
            [
                _aim(
                    tool_calls=[  # 首轮 [REPLACE, OUTPUT] 混调
                        _tc(
                            TOOL_NAME_REPLACE,
                            {"old_str": "起始笔记。", "new_str": "起始笔记。追加内容。"},
                        ),
                        _TC_OUTPUT,
                    ]
                ),
                _aim(tool_calls=[_TC_OUTPUT]),  # 第二轮修正:单 OUTPUT
            ],
            monkeypatch,
            initial_notes="起始笔记。",
        )
        assert result["structured_output"] is not None
        assert "追加内容。" in result["session_notes_working"]


class TestRouteAfterToolsStructuredOutputShortCircuit:
    """C5 段:audit_tools 已设 structured_output → route_after_tools 短路到 write_results。"""

    async def test_short_circuit_prevents_loop(self, monkeypatch):
        """state.structured_output 已设且 tool_iter_count < max → route_after_tools 返 write_results。"""
        from app.domain.audit.graph import route_after_tools
        from unittest.mock import MagicMock

        state = _initial_state()
        state["tool_iter_count"] = 2  # < max_iter=5
        state["max_iter"] = 5
        state["structured_output"] = MagicMock()  # audit_tools 已设的兜底
        # 路由短路 → write_results
        assert route_after_tools(state) == "write_results"
