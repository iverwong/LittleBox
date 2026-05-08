"""Tests for the main dialogue graph (5 nodes + 1 router).

M6 Step 6 coverage:
- Graph topology: edges load_audit_state → route_by_risk → LLM nodes → END
- inject_guidance → call_main_llm edge (guidance branch)
- route_by_risk 5-signal → 4-output priority
- inject_guidance behavior: (a) guidance before last HumanMessage,
  (b) first SystemMessage untouched, (c) guidance not in state["messages"]
- stub nodes: crisis_llm / redline_llm fall back to main + warning
- M6 always routes to "main" (all signals False)
- _assemble_llm_messages unit
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
    _assemble_llm_messages,
    call_crisis_llm,
    call_main_llm,
    call_redline_llm,
    inject_guidance,
    load_audit_state,
    main_graph,
    route_by_risk,
)
from app.chat.state import MainDialogueState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    messages: list[BaseMessage] | None = None,
    audit_state: dict | None = None,
    pending_guidance: str | None = None,
) -> MainDialogueState:
    """Minimal initial state for graph node unit tests."""
    return {
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
        "child_user_id": "child-uuid",
        "child_profile": None,  # not read in M6 nodes
        "messages": messages or [],
        "audit_state": audit_state or {},
        "pending_guidance": pending_guidance,
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


def test_graph_inject_guidance_then_main_llm():
    """guidance branch: inject_guidance → call_main_llm edge exists."""
    graph = main_graph.get_graph()
    edge_names = [(e.source, e.target) for e in graph.edges]
    assert ("inject_guidance", "call_main_llm") in edge_names


def test_graph_llm_nodes_terminate_at_end():
    """call_main_llm, call_crisis_llm, call_redline_llm all have edges → __end__."""
    graph = main_graph.get_graph()
    # Collect node names that have an edge TO __end__
    nodes_leading_to_end = {e.source for e in graph.edges if e.target == "__end__"}
    expected = {"call_main_llm", "call_crisis_llm", "call_redline_llm"}
    assert expected.issubset(nodes_leading_to_end), (
        f"{expected} not all leading to __end__. Found: {nodes_leading_to_end}"
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
    """Missing audit_state keys fallback to falsy → 'main'."""
    state = _make_state(audit_state={})
    assert route_by_risk(state) == "main"


# ---------------------------------------------------------------------------
# G3: load_audit_state — M6 stub returns all-False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_audit_state_m6_returns_all_false():
    """M6 stub: all signals False, guidance None."""
    state = _make_state()
    result = await load_audit_state(state)
    audit = result["audit_state"]
    assert audit["crisis_locked"] is False
    assert audit["crisis_detected"] is False
    assert audit["redline_triggered"] is False
    assert audit["guidance"] is None


# ---------------------------------------------------------------------------
# G4: inject_guidance — M6 stub, pending_guidance=None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_guidance_m6_returns_none():
    """M6: inject_guidance writes None to pending_guidance."""
    state = _make_state()
    result = await inject_guidance(state)
    assert result == {"pending_guidance": None}


# ---------------------------------------------------------------------------
# G5: _assemble_llm_messages — guidance injection behavior
# ---------------------------------------------------------------------------


def test_assemble_no_guidance_returns_copy():
    """pending_guidance=None → returns shallow copy of messages unchanged."""
    msgs = [
        SystemMessage(content="system"),
        HumanMessage(content="hello"),
    ]
    state = _make_state(messages=msgs, pending_guidance=None)
    result = _assemble_llm_messages(state)
    assert result == msgs
    assert result is not msgs  # is a copy


def test_assemble_guidance_before_last_human_message():
    """(a) guidance inserted immediately before the last HumanMessage."""
    msgs = [
        SystemMessage(content="system prompt"),
        HumanMessage(content="first"),
        AIMessage(content="reply"),
        HumanMessage(content="last human"),
    ]
    state = _make_state(messages=msgs, pending_guidance="please encourage")
    result = _assemble_llm_messages(state)

    guidance_msgs = [m for m in result if isinstance(m, SystemMessage) and "encourage" in m.content]
    assert len(guidance_msgs) == 1
    guidance_idx = result.index(guidance_msgs[0])
    last_human_idx = max(i for i, m in enumerate(result) if isinstance(m, HumanMessage))
    assert guidance_idx == last_human_idx - 1, (
        f"guidance at {guidance_idx}, last human at {last_human_idx}"
    )


def test_assemble_first_system_message_untouched():
    """(b) First SystemMessage is NOT modified (same object reference)."""
    sys_prompt = SystemMessage(content="do not touch this")
    msgs = [sys_prompt, HumanMessage(content="hello")]
    state = _make_state(messages=msgs, pending_guidance="some guidance")
    result = _assemble_llm_messages(state)
    assert result[0] is sys_prompt


def test_assemble_guidance_not_in_state_messages():
    """(c) state['messages'] is not modified — guidance stays in-graph only."""
    msgs = [SystemMessage(content="system"), HumanMessage(content="hello")]
    state = _make_state(messages=msgs.copy(), pending_guidance="in-graph only")
    # _assemble_llm_messages must not mutate state["messages"]
    original_len = len(state["messages"])
    _assemble_llm_messages(state)
    assert len(state["messages"]) == original_len
    assert not any("in-graph only" in str(m.content) for m in state["messages"])


def test_assemble_empty_messages_with_guidance():
    """Empty history + guidance → guidance appended as a SystemMessage (no crash)."""
    msgs: list[BaseMessage] = []
    state = _make_state(messages=msgs, pending_guidance="be nice")
    result = _assemble_llm_messages(state)
    assert len(result) == 1
    assert isinstance(result[0], SystemMessage)
    assert "be nice" in result[0].content


# ---------------------------------------------------------------------------
# G6: stub nodes — fall back to main + warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crisis_llm_falls_back_to_main_with_warning(caplog):
    """call_crisis_llm logs M6 stub warning and delegates to call_main_llm."""
    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
        pending_guidance=None,
    )
    import app.chat.graph as graph_mod

    original = graph_mod.get_stream_writer

    class FakeWriter:
        def __call__(self, data):
            pass

    graph_mod.get_stream_writer = lambda: FakeWriter()
    try:
        with caplog.at_level(logging.WARNING):
            await call_crisis_llm(state)
        assert any("M6 stub" in msg and "call_crisis_llm" in msg for msg in caplog.messages)
    finally:
        graph_mod.get_stream_writer = original


@pytest.mark.asyncio
async def test_redline_llm_falls_back_to_main_with_warning(caplog):
    """call_redline_llm logs M6 stub warning and delegates to call_main_llm."""
    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
        pending_guidance=None,
    )
    import app.chat.graph as graph_mod

    original = graph_mod.get_stream_writer

    class FakeWriter:
        def __call__(self, data):
            pass

    graph_mod.get_stream_writer = lambda: FakeWriter()
    try:
        with caplog.at_level(logging.WARNING):
            await call_redline_llm(state)
        assert any("M6 stub" in msg and "call_redline_llm" in msg for msg in caplog.messages)
    finally:
        graph_mod.get_stream_writer = original


# ---------------------------------------------------------------------------
# G7: _assemble_llm_messages returns new list
# ---------------------------------------------------------------------------


def test_assemble_llm_messages_returns_new_list():
    """_assemble_llm_messages returns a new list object, not the state list."""
    msgs = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    state = _make_state(messages=msgs, pending_guidance=None)
    result = _assemble_llm_messages(state)
    assert result is not msgs


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
    """additional_kwargs finish_reason='stop' → writer receives finish_reason stop."""

    def _fake_get_llm():
        return _FakeLLM(
            [
                AIMessageChunk(
                    content="hi",
                    additional_kwargs={"response_metadata": {"finish_reason": "stop"}},
                ),
            ]
        )

    monkeypatch.setattr("app.chat.graph.get_chat_llm", _fake_get_llm)

    written: list[dict] = []
    monkeypatch.setattr(
        "app.chat.graph.get_stream_writer",
        lambda: type("W", (), {"__call__": lambda self, d: written.append(d)})(),
    )

    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    await call_main_llm(state)

    finish_calls = [w for w in written if "finish_reason" in w]
    assert len(finish_calls) == 1
    assert finish_calls[0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_call_main_llm_finish_reason_passthrough_length(monkeypatch):
    """additional_kwargs finish_reason='length' → writer receives finish_reason length."""

    def _fake_get_llm():
        return _FakeLLM(
            [
                AIMessageChunk(
                    content="long",
                    additional_kwargs={"response_metadata": {"finish_reason": "length"}},
                ),
            ]
        )

    monkeypatch.setattr("app.chat.graph.get_chat_llm", _fake_get_llm)

    written: list[dict] = []
    monkeypatch.setattr(
        "app.chat.graph.get_stream_writer",
        lambda: type("W", (), {"__call__": lambda self, d: written.append(d)})(),
    )

    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    await call_main_llm(state)

    finish_calls = [w for w in written if "finish_reason" in w]
    assert len(finish_calls) == 1
    assert finish_calls[0]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_call_main_llm_finish_reason_passthrough_content_filter(monkeypatch):
    """additional_kwargs finish_reason='content_filter' → writer receives content_filter."""

    def _fake_get_llm():
        return _FakeLLM(
            [
                AIMessageChunk(
                    content="filtered",
                    additional_kwargs={"response_metadata": {"finish_reason": "content_filter"}},
                ),
            ]
        )

    monkeypatch.setattr("app.chat.graph.get_chat_llm", _fake_get_llm)

    written: list[dict] = []
    monkeypatch.setattr(
        "app.chat.graph.get_stream_writer",
        lambda: type("W", (), {"__call__": lambda self, d: written.append(d)})(),
    )

    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    await call_main_llm(state)

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
                    additional_kwargs={"response_metadata": {"finish_reason": "tool_calls"}},
                ),
            ]
        )

    monkeypatch.setattr("app.chat.graph.get_chat_llm", _fake_get_llm)

    written: list[dict] = []
    monkeypatch.setattr(
        "app.chat.graph.get_stream_writer",
        lambda: type("W", (), {"__call__": lambda self, d: written.append(d)})(),
    )

    state = _make_state(
        messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
    )
    await call_main_llm(state)

    finish_calls = [w for w in written if "finish_reason" in w]
    assert len(finish_calls) == 0, f"Expected no finish_reason writer call, got {finish_calls}"
