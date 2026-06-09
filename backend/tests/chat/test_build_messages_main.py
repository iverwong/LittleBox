"""build_messages_main 装配产物回归（T15）。

覆盖 W1 wrapper 模式：
1. 正常路径（guidance=None）：末位 HumanMessage.content == ctx.user_input（透传）
2. guidance 路径（guidance="..."）：末位 HumanMessage.content 含 STUB_GUIDANCE_WRAPPER 标记

依赖：db_session fixture 可用但 mock load_active_history_for_assembly 避免 DB 查询（C5 隔离铁律）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from app.domain.chat.graph import build_messages_main
from app.domain.chat.state import MainDialogueState
from langchain_core.messages import HumanMessage, SystemMessage
from tests.conftest import make_chat_context

pytestmark = pytest.mark.asyncio


def _history() -> list:
    """返回 mock 历史消息列表（由 load_active_history_for_assembly 假返回）。"""
    return [
        HumanMessage(content="昨天我们聊了什么？"),
        HumanMessage(content="今天心情怎么样？"),
    ]


async def test_build_messages_main_assembles_system_and_history():
    """Given guidance=None, When W1 wrapper, Then 末位 HumanMessage.content == user_input（透传）。"""
    from types import SimpleNamespace

    ctx = make_chat_context(
        session_id="00000000-0000-0000-0000-000000000001",
        child_user_id="00000000-0000-0000-0000-000000000002",
        user_input="我今天很开心",
        settings=MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=MagicMock(),
    )
    state: MainDialogueState = {
        "messages": [], "audit_state": {"crisis_locked": False,
                                         "crisis_detected": False,
                                         "redline_triggered": False,
                                         "guidance": None,
                                         "target_message_id": None},
        "generated_token_count": 0, "client_alive": True,
        "user_stop_requested": False, "turn_number": 1,
    }
    runtime = SimpleNamespace(context=ctx)

    with patch("app.domain.chat.graph.load_active_history_for_assembly", return_value=_history()):
        result = await build_messages_main(state, runtime)

    msgs = result["messages"]
    # 首条应为 SystemPrompt（来自 build_system_prompt）
    assert isinstance(msgs[0], SystemMessage), "首条消息应为 SystemMessage"
    # 历史消息应完整保留
    assert any(isinstance(m, HumanMessage) and "昨天我们聊了什么" in m.content
               for m in msgs)
    assert any(isinstance(m, HumanMessage) and "今天心情怎么样" in m.content
               for m in msgs)
    # 末位 HumanMessage.content == user_input（guidance=None 透传）
    last = msgs[-1]
    assert isinstance(last, HumanMessage)
    assert last.content == "我今天很开心"


async def test_build_messages_main_with_guidance():
    """Given guidance 非空, When W1 wrapper, Then 末位 HumanMessage.content 含 guidance 标记 + user_input。"""
    from types import SimpleNamespace

    ctx = make_chat_context(
        session_id="00000000-0000-0000-0000-000000000001",
        child_user_id="00000000-0000-0000-0000-000000000002",
        user_input="我想玩游戏",
        settings=MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=MagicMock(),
    )
    state: MainDialogueState = {
        "messages": [], "audit_state": {"crisis_locked": False,
                                         "crisis_detected": False,
                                         "redline_triggered": False,
                                         "guidance": "建议引导到户外活动",
                                         "target_message_id": None},
        "generated_token_count": 0, "client_alive": True,
        "user_stop_requested": False, "turn_number": 1,
    }
    runtime = SimpleNamespace(context=ctx)

    with patch("app.domain.chat.graph.load_active_history_for_assembly", return_value=_history()):
        result = await build_messages_main(state, runtime)

    msgs = result["messages"]
    # 末位 HumanMessage.content 含 STUB 标记
    last = msgs[-1]
    assert isinstance(last, HumanMessage)
    assert "TODO(prompts-content)" in last.content, "guidance 非空时末位 HumanMessage 应含 STUB 标记"
    assert "我想玩游戏" in last.content, "user_input 应在 wrapper 内"
    assert "建议引导到户外活动" in last.content, "guidance 应在 wrapper 内"
