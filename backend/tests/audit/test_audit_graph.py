"""审查 LangGraph agentic loop 测试：7 路径覆盖 + D11 v3 post-processing 测试。"""
from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from app.audit.graph import (
    AuditGraphState,
    TOOL_NAME_APPEND,
    TOOL_NAME_OUTPUT,
    TOOL_NAME_REPLACE,
    build_audit_graph,
)

pytestmark = [
    pytest.mark.audit,
    pytest.mark.asyncio,
]


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
    "guidance": "观察情绪走向",
    "turn_summary": "情绪稳定",
}

_TC_OUTPUT = _tc(TOOL_NAME_OUTPUT, _AUDIT_OUTPUT_ARGS)
_TC_APPEND = _tc(TOOL_NAME_APPEND, {"text": "追加的笔记。"})
_TC_REPLACE = _tc(TOOL_NAME_REPLACE, {"old_str": "情绪稳定", "new_str": "情绪有些波动"})
_TC_REPLACE_MISS = _tc(TOOL_NAME_REPLACE, {"old_str": "不存在的文本", "new_str": "替换"})
_TC_REPLACE_MULTI = _tc(TOOL_NAME_REPLACE, {"old_str": "a", "new_str": "b"})


def _initial_state() -> AuditGraphState:
    return {
        "sid": "session-1",
        "turn_number": 1,
        "child_profile": None,
        "session_notes_working": "用户今天情绪稳定。",
        "tool_iter_count": 0,
        "structured_output": None,
        "messages": [],
    }


def _run(
    responses: list[AIMessage],
    monkeypatch: pytest.MonkeyPatch,
    max_iter: int = 5,
    exhausted_raises: bool = False,
) -> dict:
    """构造 graph + 注入 fake LLM + 调用 ainvoke，返回终态。"""
    fake = FakeAuditLLM(responses, exhausted_raises=exhausted_raises)
    monkeypatch.setattr("app.audit.graph.build_audit_llm", lambda s: fake)
    # mock 数据层调用为 no-op
    async def _mock_load(*_): return []
    monkeypatch.setattr("app.audit.graph._load_messages_from_pg", _mock_load)
    async def _mock_write(*_, **__): pass
    monkeypatch.setattr("app.audit.graph.write_audit_results", _mock_write)

    graph = build_audit_graph(max_iter=max_iter)
    state = _initial_state()
    result = graph.ainvoke(state, {"configurable": {"settings": None}})
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
            "sid": "session-1", "turn_number": 1, "child_profile": None,
            "session_notes_working": "a a b",  # "a" 出现 2 次
            "tool_iter_count": 0, "structured_output": None, "messages": [],
        }

        fake = FakeAuditLLM([
            _aim(tool_calls=[_TC_REPLACE_MULTI]),
            _aim(tool_calls=[_tc(TOOL_NAME_REPLACE, {"old_str": "a a", "new_str": "b b"})]),
            _aim(tool_calls=[_TC_OUTPUT]),
        ])
        monkeypatch.setattr("app.audit.graph.build_audit_llm", lambda s: fake)
        async def _mock_load(*_: Any) -> list: return []
        monkeypatch.setattr("app.audit.graph._load_messages_from_pg", _mock_load)
        async def _mock_write(*_: Any, **__: Any) -> None: pass
        monkeypatch.setattr("app.audit.graph.write_audit_results", _mock_write)

        graph = build_audit_graph()
        result = await graph.ainvoke(state, {"configurable": {"settings": None}})

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
        assert result["structured_output"].guidance == "审查循环超限，已降级"
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
        assert result["structured_output"].guidance == "模型未能给出结构化结论"
