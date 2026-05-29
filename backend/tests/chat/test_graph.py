"""Tests for the main dialogue graph (7 nodes + 4-branch router).

M6 Step 6 coverage:
- Graph topology: edges load_audit_state → route_by_risk → assembly → LLM → END
- 7 nodes + 4 conditional edges (crisis / redline / guidance / main)
- route_by_risk 5-signal → 4-output priority
- stub nodes: crisis_llm / redline_llm fall back to main + warning
- M8 always routes to "main" (all signals False)
"""

import logging

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from app.chat.graph import (
    build_main_graph,
    call_crisis_llm,
    call_main_llm,
    call_redline_llm,
    load_audit_state,
    route_by_risk,
)
from app.chat.state import MainDialogueState
from tests.chat.test_load_audit_state import _make_fake_runtime as _mk_runtime

main_graph = build_main_graph()

# call_main_llm / load_audit_state 测试通用 fake runtime（minimal，mock 优先于真实调用）
_FAKE_RUNTIME = _mk_runtime()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ALL_FALSE_AUDIT = {
    "crisis_locked": False,
    "crisis_detected": False,
    "redline_triggered": False,
    "guidance": None,
    "target_message_id": None,
}


def _make_state(
    *,
    messages: list[BaseMessage] | None = None,
    audit_state: dict | None = None,
    provider: str = "deepseek",
) -> MainDialogueState:
    """Minimal initial state for graph node unit tests."""
    return {
        "messages": messages or [],
        "audit_state": audit_state or _ALL_FALSE_AUDIT,
        "generated_token_count": 0,
        "client_alive": True,
        "user_stop_requested": False,
    }


# ---------------------------------------------------------------------------
# G1: Graph topology — compiled graph has correct edges
# ---------------------------------------------------------------------------


def test_graph_has_load_audit_state_entry_point():
    """START must point to load_audit_state."""
    graph = main_graph.get_graph()
    edges_from_start = [edge for edge in graph.edges if edge.source == "__start__"]
    assert len(edges_from_start) == 1
    assert edges_from_start[0].target == "load_audit_state"


def test_graph_llm_nodes_terminate_at_end():
    """call_main_llm, call_crisis_llm, call_redline_llm all have edges → __end__."""
    graph = main_graph.get_graph()
    # Collect node names that have an edge TO __end__
    nodes_leading_to_end = {e.source for e in graph.edges if e.target == "__end__"}
    expected = {"call_main_llm", "call_crisis_llm", "call_redline_llm"}
    assert expected.issubset(nodes_leading_to_end), (
        f"{expected} not all leading to __end__. Found: {nodes_leading_to_end}"
    )


def test_graph_has_7_nodes():
    """7 个注册节点 + 条件路由（C6：节点拓扑完整性）。"""
    graph = main_graph.get_graph()
    node_names = set(graph.nodes.keys())
    expected = {
        "load_audit_state",
        "build_messages_main",
        "build_messages_crisis",
        "build_messages_redline",
        "call_main_llm",
        "call_crisis_llm",
        "call_redline_llm",
    }
    assert expected.issubset(node_names), (
        f"Missing nodes: {expected - node_names}"
    )


def test_graph_no_db_write_nodes():
    """No DB-persistence node exists in the graph (helpers are outside the graph)."""
    graph = main_graph.get_graph()
    node_names = set(graph.nodes.keys())
    assert "persist_ai_turn" not in node_names
    assert "enqueue_audit" not in node_names


# ---------------------------------------------------------------------------
# G2: route_by_risk — 5 signals → 4 outputs priority
# ---------------------------------------------------------------------------


def test_route_by_risk_crisis_locked_highest_priority():
    """crisis_locked=true + redline_triggered=true → crisis wins (priority ①)."""
    state = _make_state(
        audit_state={
            "crisis_locked": True,
            "crisis_detected": False,
            "redline_triggered": True,
            "guidance": "some guidance",
        }
    )
    assert route_by_risk(state) == "crisis"


def test_route_by_risk_crisis_detected_over_redline():
    """crisis_detected=true + redline_triggered=true → crisis wins (priority ② > ③)."""
    state = _make_state(
        audit_state={
            "crisis_locked": False,
            "crisis_detected": True,
            "redline_triggered": True,
            "guidance": "some guidance",
        }
    )
    assert route_by_risk(state) == "crisis"


def test_route_by_risk_redline_over_guidance():
    """redline_triggered=true + guidance!=None → redline wins (priority ③ > ④)."""
    state = _make_state(
        audit_state={
            "crisis_locked": False,
            "crisis_detected": False,
            "redline_triggered": True,
            "guidance": "please be careful",
        }
    )
    assert route_by_risk(state) == "redline"


def test_route_by_risk_guidance_branch():
    """guidance!=None, all others false → guidance."""
    state = _make_state(
        audit_state={
            "crisis_locked": False,
            "crisis_detected": False,
            "redline_triggered": False,
            "guidance": "please be encouraging",
        }
    )
    assert route_by_risk(state) == "guidance"


def test_route_by_risk_m6_always_main():
    """M6: all audit_state values are False/None → always 'main'."""
    state = _make_state(
        audit_state={
            "crisis_locked": False,
            "crisis_detected": False,
            "redline_triggered": False,
            "guidance": None,
        }
    )
    assert route_by_risk(state) == "main"


def test_route_by_risk_empty_audit_state():
    """AuditState 默认全 False → 路由到 'main'。"""
    state = _make_state()
    assert route_by_risk(state) == "main"


# ---------------------------------------------------------------------------
# G3: load_audit_state — M6 stub returns all-False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_audit_state_m6_returns_all_false():
    """M6 stub: all signals False, guidance None."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    # 最小 fake runtime（首轮不进 poll_wait，只需 .context 存在）
    runtime = SimpleNamespace(
        context=SimpleNamespace(
            session_id="test-sid",
            audit_redis=AsyncMock(),
            settings=SimpleNamespace(
                audit_redis_ttl_seconds=86400,
                audit_wait_timeout_seconds=30,
            ),
        ),
    )
    state = _make_state()
    result = await load_audit_state(state, runtime)
    audit = result["audit_state"]
    assert audit["crisis_locked"] is False
    assert audit["crisis_detected"] is False
    assert audit["redline_triggered"] is False
    assert audit["guidance"] is None


def _make_stub_runtime():
    """最小 fake runtime（调用方不关心 stream 输出时用）。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.config import settings as _app_settings

    return SimpleNamespace(
        context=SimpleNamespace(
            settings=_app_settings,
            audit_redis=AsyncMock(),
            session_id="test-sid",
            child_user_id="child-uuid",
            child_profile={},
            age=8,
            gender=None,
            user_input="test",
            db_session_factory=AsyncMock(),
        ),
    )


# ---------------------------------------------------------------------------
# G6: stub nodes — fall back to main + warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crisis_llm_streams_via_writer(monkeypatch):
    """call_crisis_llm 通过 stream writer 输出 delta + finish_reason。"""
    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    runtime = _make_stub_runtime()

    written: list[dict] = []
    fake_writer = type("W", (), {"__call__": lambda self, d: written.append(d)})()

    fake_chunk = AIMessageChunk(
        content="回复",
        response_metadata={"finish_reason": "stop"},
    )

    class _FakeLLM:
        async def astream(self, msgs):
            yield fake_chunk

    monkeypatch.setattr("app.chat.graph.get_stream_writer", lambda: fake_writer)
    monkeypatch.setattr("app.chat.graph.build_crisis_llm", lambda _: _FakeLLM())

    await call_crisis_llm(state, runtime)

    deltas = [w for w in written if "delta" in w]
    assert len(deltas) >= 1
    assert deltas[0]["delta"] == "回复"


@pytest.mark.asyncio
async def test_redline_llm_streams_via_writer(monkeypatch):
    """call_redline_llm 通过 stream writer 输出 delta + finish_reason。"""
    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    runtime = _make_stub_runtime()

    written: list[dict] = []
    fake_writer = type("W", (), {"__call__": lambda self, d: written.append(d)})()

    fake_chunk = AIMessageChunk(
        content="红线回复",
        response_metadata={"finish_reason": "stop"},
    )

    class _FakeLLM:
        async def astream(self, msgs):
            yield fake_chunk

    monkeypatch.setattr("app.chat.graph.get_stream_writer", lambda: fake_writer)
    monkeypatch.setattr("app.chat.graph.build_redline_llm", lambda _: _FakeLLM())

    await call_redline_llm(state, runtime)

    deltas = [w for w in written if "delta" in w]
    assert len(deltas) >= 1
    assert deltas[0]["delta"] == "红线回复"


# ---------------------------------------------------------------------------
# G8: call_main_llm — finish_reason passthrough (X-2 gate)
# ---------------------------------------------------------------------------


class _FakeLLM:
    """Wrap a list of AIMessageChunk with astream() for llm mock."""

    def __init__(self, chunks: list[AIMessageChunk]):
        self._chunks = chunks

    async def astream(self, messages):
        for c in self._chunks:
            yield c


@pytest.mark.asyncio
async def test_call_main_llm_finish_reason_passthrough_stop(monkeypatch):
    """finish_reason='stop' → writer receives finish_reason stop."""

    def _fake_get_llm():
        return _FakeLLM(
            [
                AIMessageChunk(
                    content="hi",
                    response_metadata={"finish_reason": "stop"},
                ),
            ]
        )

    monkeypatch.setattr("app.chat.graph.build_main_llm", lambda _: _fake_get_llm())

    written: list[dict] = []
    monkeypatch.setattr(
        "app.chat.graph.get_stream_writer",
        lambda: type("W", (), {"__call__": lambda self, d: written.append(d)})(),
    )

    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    await call_main_llm(state, _FAKE_RUNTIME)

    finish_calls = [w for w in written if "finish_reason" in w]
    assert len(finish_calls) == 1
    assert finish_calls[0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_call_main_llm_finish_reason_passthrough_length(monkeypatch):
    """finish_reason='length' → writer receives finish_reason length."""

    def _fake_get_llm():
        return _FakeLLM(
            [
                AIMessageChunk(
                    content="long",
                    response_metadata={"finish_reason": "length"},
                ),
            ]
        )

    monkeypatch.setattr("app.chat.graph.build_main_llm", lambda _: _fake_get_llm())

    written: list[dict] = []
    monkeypatch.setattr(
        "app.chat.graph.get_stream_writer",
        lambda: type("W", (), {"__call__": lambda self, d: written.append(d)})(),
    )

    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    await call_main_llm(state, _FAKE_RUNTIME)

    finish_calls = [w for w in written if "finish_reason" in w]
    assert len(finish_calls) == 1
    assert finish_calls[0]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_call_main_llm_finish_reason_passthrough_content_filter(monkeypatch):
    """finish_reason='content_filter' → writer receives content_filter."""

    def _fake_get_llm():
        return _FakeLLM(
            [
                AIMessageChunk(
                    content="filtered",
                    response_metadata={"finish_reason": "content_filter"},
                ),
            ]
        )

    monkeypatch.setattr("app.chat.graph.build_main_llm", lambda _: _fake_get_llm())

    written: list[dict] = []
    monkeypatch.setattr(
        "app.chat.graph.get_stream_writer",
        lambda: type("W", (), {"__call__": lambda self, d: written.append(d)})(),
    )

    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    await call_main_llm(state, _FAKE_RUNTIME)

    finish_calls = [w for w in written if "finish_reason" in w]
    assert len(finish_calls) == 1
    assert finish_calls[0]["finish_reason"] == "content_filter"


@pytest.mark.asyncio
async def test_call_main_llm_finish_reason_non_whitelist_filtered(monkeypatch):
    """finish_reason='tool_calls' (not whitelisted) → writer NOT called with finish_reason."""

    def _fake_get_llm():
        return _FakeLLM(
            [
                AIMessageChunk(
                    content="tool call",
                    response_metadata={"finish_reason": "tool_calls"},
                ),
            ]
        )

    monkeypatch.setattr("app.chat.graph.build_main_llm", lambda _: _fake_get_llm())

    written: list[dict] = []
    monkeypatch.setattr(
        "app.chat.graph.get_stream_writer",
        lambda: type("W", (), {"__call__": lambda self, d: written.append(d)})(),
    )

    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    await call_main_llm(state, _FAKE_RUNTIME)

    finish_calls = [w for w in written if "finish_reason" in w]
    assert len(finish_calls) == 0, f"Expected no finish_reason writer call, got {finish_calls}"


# ---------------------------------------------------------------------------
# G6: reasoning signal passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_main_llm_emits_reasoning_signal_on_reasoning_content(monkeypatch):
    """chunk.additional_kwargs reasoning_content → writer receives {'reasoning': True}."""

    def _fake_get_llm():
        return _FakeLLM(
            [
                AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": "思考中..."},
                ),
                AIMessageChunk(
                    content="结论",
                    additional_kwargs={},
                ),
            ]
        )

    monkeypatch.setattr("app.chat.graph.build_main_llm", lambda _: _fake_get_llm())

    written: list[dict] = []
    monkeypatch.setattr(
        "app.chat.graph.get_stream_writer",
        lambda: type("W", (), {"__call__": lambda self, d: written.append(d)})(),
    )

    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    await call_main_llm(state, _FAKE_RUNTIME)

    reasoning_calls = [w for w in written if w.get("reasoning") is True]
    assert len(reasoning_calls) == 1, f"Expected 1 reasoning signal, got {reasoning_calls}"
    assert "delta" not in reasoning_calls[0], "Reasoning signal must not carry delta text"

    # Second chunk (no reasoning_content) must NOT emit reasoning signal
    delta_calls = [w for w in written if "delta" in w]
    assert len(delta_calls) == 1
    assert delta_calls[0]["delta"] == "结论"


@pytest.mark.asyncio
async def test_call_main_llm_no_reasoning_no_signal(monkeypatch):
    """chunk without reasoning_content → writer NOT called with reasoning signal."""

    def _fake_get_llm():
        return _FakeLLM(
            [
                AIMessageChunk(content="hi", additional_kwargs={}),
            ]
        )

    monkeypatch.setattr("app.chat.graph.build_main_llm", lambda _: _fake_get_llm())

    written: list[dict] = []
    monkeypatch.setattr(
        "app.chat.graph.get_stream_writer",
        lambda: type("W", (), {"__call__": lambda self, d: written.append(d)})(),
    )

    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    await call_main_llm(state, _FAKE_RUNTIME)

    reasoning_calls = [w for w in written if w.get("reasoning") is True]
    assert len(reasoning_calls) == 0, f"Expected no reasoning signal, got {reasoning_calls}"
