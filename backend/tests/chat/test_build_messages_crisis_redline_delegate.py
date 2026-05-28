"""crisis/redline 委派 main 等价硬断言（T15 M1）。

mock.patch("app.chat.graph.build_messages_main") 拦截，断言：
- call_crisis_llm 透传调 build_messages_main 一次
- call_redline_llm 透传调 build_messages_main 一次
- 返回值 == mock 返回值（保证零变化）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.chat.graph import build_messages_crisis, build_messages_redline
from app.chat.state import MainDialogueState

pytestmark = pytest.mark.asyncio

_STATE: MainDialogueState = {
    "session_id": "s1", "child_user_id": "c1", "provider": "deepseek",
    "messages": [], "audit_state": {"crisis_locked": False,
                                     "crisis_detected": False,
                                     "redline_triggered": False,
                                     "guidance": None},
    "generated_token_count": 0, "client_alive": True,
    "user_stop_requested": False, "turn_number": 1,
}

_RUNTIME = None  # 函数不消费 runtime，传递 None 验证透传


async def test_crisis_delegates_to_main():
    """build_messages_crisis 透传调 build_messages_main，返回值一致。"""
    fake_result = {"messages": [object()]}
    with patch("app.chat.graph.build_messages_main", return_value=fake_result) as mock_main:
        result = await build_messages_crisis(_STATE, _RUNTIME)
        mock_main.assert_awaited_once_with(_STATE, _RUNTIME)
        assert result is fake_result, "返回值应与 build_messages_main 相同"


async def test_redline_delegates_to_main():
    """build_messages_redline 透传调 build_messages_main，返回值一致。"""
    fake_result = {"messages": [object()]}
    with patch("app.chat.graph.build_messages_main", return_value=fake_result) as mock_main:
        result = await build_messages_redline(_STATE, _RUNTIME)
        mock_main.assert_awaited_once_with(_STATE, _RUNTIME)
        assert result is fake_result, "返回值应与 build_messages_main 相同"
