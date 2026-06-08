"""审查 LangGraph agentic loop 测试：7 路径覆盖 + D11 v3 post-processing 测试。

T11+T12（D-patch0-6）：build_audit_graph() 无参工厂 + Runtime[AuditContextSchema] DI。
测试通过 _make_fake_runtime 构造 fake Runtime 注入 AuditContextSchema 模拟资源。
"""
from __future__ import annotations

from typing import Any

import pytest
from app.domain.audit.graph import (
    TOOL_NAME_APPEND,
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
    "emotional": 0, "social": 0, "romance": 0, "values": 0,
    "boundaries": 0, "academic": 0, "lifestyle": 0,
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
_TC_APPEND = _tc(TOOL_NAME_APPEND, {"text": "追加的笔记。"})
_TC_REPLACE = _tc(TOOL_NAME_REPLACE, {"old_str": "情绪稳定", "new_str": "情绪有些波动"})
_TC_REPLACE_MISS = _tc(TOOL_NAME_REPLACE, {"old_str": "不存在的文本", "new_str": "替换"})
_TC_REPLACE_MULTI = _tc(TOOL_NAME_REPLACE, {"old_str": "a", "new_str": "b"})


def _initial_state() -> AuditGraphState:
    return {
        "sid": SID,
        "turn_number": 1,
        "child_profile": None,
        "session_notes_working": "用户今天情绪稳定。",
        "tool_iter_count": 0,
        "structured_output": None,
        "messages": [],
        "max_iter": 5,  # D-patch0-6：路由函数妥协，由 load_context 写入
    }


def _make_fake_runtime(max_iter: int = 5) -> object:
    """构造最小 Runtime[AuditContextSchema] 替代（LangGraph 注入 mock）。

    T11（D-patch0-6）：audit 侧 Runtime DI 测试范式，
    对齐 tests/chat/test_load_audit_state.py::_make_fake_runtime。
    测试中直接调节点函数 (state, runtime)，runtime 仅提供 .context 属性。
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.domain.audit.context_schema import AuditContextSchema

    ctx = AuditContextSchema(
        session_id=SID,
        child_user_id=CUID,
        target_message_id=TARGET_MID,
        max_iter=max_iter,
        settings=MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=MagicMock(),
    )
    return SimpleNamespace(context=ctx)


def _run(
    responses: list[AIMessage],
    monkeypatch: pytest.MonkeyPatch,
    max_iter: int = 5,
    exhausted_raises: bool = False,
) -> dict:
    """构造 graph + 注入 fake LLM + 调用 ainvoke，返回终态。

    T11：build_audit_graph() 无参调用 + context= 参数传递。
    """
    fake = FakeAuditLLM(responses, exhausted_raises=exhausted_raises)
    monkeypatch.setattr("app.domain.audit.graph.build_audit_llm", lambda s: fake)
    # mock 数据层调用为 no-op
    async def _mock_load(*_: Any, **__: Any) -> list:
        return []
    monkeypatch.setattr("app.domain.audit.graph._load_messages_from_pg", _mock_load)
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

    async def test_one_append(self, monkeypatch):
        """路径 ②：1 append → 正确追加到 session_notes → AuditOutputSchema 终止。"""
        result = await _run([
            _aim(tool_calls=[_TC_APPEND]),
            _aim(tool_calls=[_TC_OUTPUT]),
        ], monkeypatch)
        assert "追加的笔记。" in result["session_notes_working"]
        assert result["structured_output"] is not None

    async def test_one_replace_hit(self, monkeypatch):
        """路径 ③：1 replace 唯一命中 → 替换成功 → 终止。"""
        result = await _run([
            _aim(tool_calls=[_TC_REPLACE]),
            _aim(tool_calls=[_TC_OUTPUT]),
        ], monkeypatch)
        assert "情绪有些波动" in result["session_notes_working"]
        assert "情绪稳定" not in result["session_notes_working"]
        assert result["structured_output"] is not None

    async def test_replace_miss_then_append(self, monkeypatch):
        """路径 ④：replace 0 命中 → LLM 改调 append → 通过。"""
        result = await _run([
            _aim(tool_calls=[_TC_REPLACE_MISS]),
            _aim(tool_calls=[_TC_APPEND]),
            _aim(tool_calls=[_TC_OUTPUT]),
        ], monkeypatch)
        # 第一轮 replace 不命中 → session_notes 不变
        assert "用户今天情绪稳定。" in result["session_notes_working"]
        # 第二轮 append 追加
        assert "追加的笔记。" in result["session_notes_working"]
        assert result["structured_output"] is not None
        assert result["tool_iter_count"] == 2

    async def test_replace_multi_then_precise(self, monkeypatch):
        """路径 ⑤：replace ≥2 命中 → LLM 用更精确 old_str 重试 → 1 命中。"""
        state: AuditGraphState = {
            "sid": SID, "turn_number": 1, "child_profile": None,
            "session_notes_working": "a a b",  # "a" 出现 2 次
            "tool_iter_count": 0, "structured_output": None, "messages": [],
            "max_iter": 5,
        }

        fake = FakeAuditLLM([
            _aim(tool_calls=[_TC_REPLACE_MULTI]),
            _aim(tool_calls=[_tc(TOOL_NAME_REPLACE, {"old_str": "a a", "new_str": "b b"})]),
            _aim(tool_calls=[_TC_OUTPUT]),
        ])
        monkeypatch.setattr("app.domain.audit.graph.build_audit_llm", lambda s: fake)
        async def _mock_load(*_: Any, **__: Any) -> list:
            return []
        monkeypatch.setattr("app.domain.audit.graph._load_messages_from_pg", _mock_load)
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
        """路径 ⑥：连续 replace 失败 → 循环超限 → 降级 append + 降级标记。"""
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
        assert result["structured_output"].turn_summary == "审查超时降级"
        assert "原始建议如下" in result["session_notes_working"]

    async def test_mixed_append_replace(self, monkeypatch):
        """路径 ⑦：多轮交替 append + replace → session_notes_working 状态正确累积。"""
        result = await _run([
            _aim(tool_calls=[_TC_APPEND]),
            _aim(tool_calls=[_TC_REPLACE]),
            _aim(tool_calls=[_tc(TOOL_NAME_APPEND, {"text": "最终观察。"})]),
            _aim(tool_calls=[_TC_OUTPUT]),
        ], monkeypatch)
        notes = result["session_notes_working"]
        assert "追加的笔记。" in notes
        assert "情绪有些波动" in notes  # replace 替换了"情绪稳定"→"情绪有些波动"
        assert "最终观察。" in notes
        assert result["tool_iter_count"] == 3
        assert result["structured_output"] is not None


class TestPostProcessing:
    """D11 v3 post-processing 兜底测试。"""

    async def test_followup_triggers_on_missing_audit_output(self, monkeypatch):
        """模型首轮未调 audit_output → post-processing 触发追问 → 第二轮调了 → verdict 正确解析。"""
        result = await _run([
            _aim(tool_calls=[_TC_APPEND]),  # 首轮只调了 append，没收尾
            # 追问后自动获得默认 audit_output 响应
        ], monkeypatch)
        # post-processing 追问后调了 audit_output
        assert result["structured_output"] is not None

    async def test_double_fail_degradation(self, monkeypatch):
        """模型两轮都未调 audit_output → 走 default verdict=warn 分支 + 日志告警。"""
        result = await _run(
            [
                _aim(content="我觉得这个对话很正常"),  # 首轮纯文本
                _aim(content="好的我再想想"),           # 追问后仍纯文本
            ],
            monkeypatch,
            exhausted_raises=True,  # 不允许默认响应
        )
        assert result["structured_output"] is not None
        # M9.5 契约：模型两轮都未给结构化结论时，guidance_injection 必须为 None
        assert result["structured_output"].guidance_injection is None
        # 降级覆盖迁到 turn_summary，保留诊断信息
        assert result["structured_output"].turn_summary == "审查降级：模型未调用 audit_output"
