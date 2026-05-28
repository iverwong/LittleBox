"""审查图节点 Runtime DI 直接断言（T16 H5）。

去重边界（H5）：不通过整图 ainvoke，直接节点函数级测试：
- load_context：验证 _load_messages_from_pg 被调用时第二个参数 == runtime.context.db_session_factory
- audit_llm_call：验证 build_audit_llm 被调用时参数 == runtime.context.settings
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.audit.graph import AuditGraphState, load_context, audit_llm_call

pytestmark = [pytest.mark.audit, pytest.mark.asyncio]

SID = "00000000-0000-0000-0000-000000000001"
CUID = "00000000-0000-0000-0000-000000000002"


def _make_state(**overrides: object) -> AuditGraphState:
    state: AuditGraphState = {
        "sid": SID,
        "turn_number": 1,
        "child_profile": None,
        "session_notes_working": "",
        "tool_iter_count": 0,
        "structured_output": None,
        "messages": [],
        "max_iter": 5,
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


def _make_fake_runtime() -> object:
    """构造最小 Runtime[AuditContextSchema] 替代（SimpleNamespace 范式）。"""
    from types import SimpleNamespace

    from app.audit.context_schema import AuditContextSchema

    ctx = AuditContextSchema(
        session_id=SID,
        child_user_id=CUID,
        max_iter=5,
        settings=MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=MagicMock(),
    )
    return SimpleNamespace(context=ctx)


async def test_load_context_passes_db_session_factory():
    """load_context 调 _load_messages_from_pg 时传入 ctx.db_session_factory。"""
    state = _make_state()
    runtime = _make_fake_runtime()

    with patch("app.audit.graph._load_messages_from_pg", return_value=[]) as mock_load:
        result = await load_context(state, runtime)

    # _load_messages_from_pg 被调一次，第二个参数 == runtime.context.db_session_factory
    mock_load.assert_awaited_once()
    args, _ = mock_load.await_args
    assert len(args) >= 2
    assert args[1] is runtime.context.db_session_factory, (
        "第二个参数应为 runtime.context.db_session_factory"
    )
    # 返回值含 max_iter
    assert result.get("max_iter") == 5


async def test_load_context_returns_messages_with_max_iter():
    """load_context 返回 dict 含 messages 和 max_iter。"""
    state = _make_state()
    runtime = _make_fake_runtime()

    with patch("app.audit.graph._load_messages_from_pg", return_value=[]):
        result = await load_context(state, runtime)

    assert "messages" in result
    assert "max_iter" in result
    assert result["max_iter"] == 5


async def test_audit_llm_call_passes_settings():
    """audit_llm_call 调 build_audit_llm 时传入 ctx.settings。

    验证 Runtime DI 正确注入 settings 参数（M8 期 closure 注入替代）。
    """
    state = _make_state()
    runtime = _make_fake_runtime()
    from langchain_core.messages import AIMessage

    with patch("app.audit.graph.build_audit_llm") as mock_build:
        # 首次 ainvoke 返回纯文本（触发 post-processing 追问）
        first_ai = AIMessage(content="需要分析一下", tool_calls=[])
        # 第二次 ainvoke 返回 audit_output
        second_ai = AIMessage(
            content="",
            tool_calls=[{
                "name": "AuditOutputSchema", "args": {
                    "dimension_scores": {"emotional": 0, "social": 0, "romance": 0,
                                          "values": 0, "boundaries": 0,
                                          "academic": 0, "lifestyle": 0},
                    "crisis_detected": False, "crisis_topic": None,
                    "redline_triggered": False, "redline_detail": None,
                    "guidance": "ok", "turn_summary": "ok",
                }, "id": "call-2",
            }],
        )

        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[first_ai, second_ai])
        mock_build.return_value = mock_llm

        result = await audit_llm_call(state, runtime)

    # build_audit_llm 被调一次，参数 == runtime.context.settings
    mock_build.assert_called_once_with(runtime.context.settings)
    # 终态 messages 末尾含 AIMessage（post-processing 后调了 audit_output）
    assert "messages" in result
    msgs = result["messages"]
    assert len(msgs) >= 1
    last_msg = msgs[-1]
    assert hasattr(last_msg, "tool_calls") and len(last_msg.tool_calls) > 0, (
        "末条消息应为含 tool_calls 的 AIMessage"
    )
    assert last_msg.tool_calls[0]["name"] == "AuditOutputSchema"
