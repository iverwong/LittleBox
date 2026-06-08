"""crisis/redline build_messages 真装配断言（M9 Step 7）。

mock context builder + runtime，验证装配顺序和 wrapper 注入。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from app.domain.chat.graph import build_messages_crisis, build_messages_redline
from app.domain.chat.state import MainDialogueState
from langchain_core.messages import HumanMessage, SystemMessage

pytestmark = pytest.mark.asyncio

_STATE: MainDialogueState = {
    "messages": [], "audit_state": {"crisis_locked": False,
                                     "crisis_detected": False,
                                     "redline_triggered": False,
                                     "guidance": None,
                                     "target_message_id": "00000000-0000-0000-0000-000000000001"},
    "generated_token_count": 0, "client_alive": True,
    "user_stop_requested": False, "turn_number": 3,
}


def _make_fake_ctx():
    from types import SimpleNamespace
    return SimpleNamespace(
        session_id="sid", user_input="我很难过", age=10, gender="male",
        settings=MagicMock(), db_session_factory=MagicMock(),
    )


async def test_crisis_build_messages_expected_order():
    """Given crisis state, When build_messages_crisis, Then 装配系统 + anchor + after + wrapper 顺序。"""
    ctx = _make_fake_ctx()
    runtime = MagicMock(context=ctx)
    fake_anchor = SystemMessage(content="[anchor 窗口]\n...")
    fake_after = [HumanMessage(content="历史消息")]

    with (
        patch("app.domain.chat.graph.build_crisis_context", return_value=(fake_anchor, fake_after)),
        patch("app.domain.chat.graph.build_crisis_system_prompt", return_value=SystemMessage(content="[crisis system]")),
    ):
        result = await build_messages_crisis(_STATE, runtime)

    msgs = result["messages"]
    assert isinstance(msgs[0], SystemMessage)
    assert msgs[1] is fake_anchor
    assert msgs[2] is fake_after[0]
    assert isinstance(msgs[-1], HumanMessage)
    assert "我很难过" in msgs[-1].content


async def test_redline_build_messages_expected_order():
    """Given redline state, When build_messages_redline, Then 装配系统 + summaries + pairs + wrapper 顺序。"""
    ctx = _make_fake_ctx()
    runtime = MagicMock(context=ctx)
    fake_summaries = [SystemMessage(content="sum1"), SystemMessage(content="sum2")]
    fake_pairs = [HumanMessage(content="前三轮消息")]

    with (
        patch("app.domain.chat.graph.build_redline_context", return_value=(fake_summaries, fake_pairs)),
        patch("app.domain.chat.graph.build_redline_system_prompt", return_value=SystemMessage(content="[redline system]")),
    ):
        result = await build_messages_redline(_STATE, runtime)

    msgs = result["messages"]
    assert isinstance(msgs[0], SystemMessage)
    assert msgs[1] is fake_summaries[0]
    assert msgs[2] is fake_summaries[1]
    pair_start = 1 + len(fake_summaries)
    assert msgs[pair_start] is fake_pairs[0]
    assert isinstance(msgs[-1], HumanMessage)
    assert "我很难过" in msgs[-1].content
