"""build_messages_main 装配产物回归（T15）。

覆盖两种模式：
1. 正常模式：SystemMessage + 历史消息 + 本轮 human
2. guidance 模式：末位 HumanMessage 前插入 SystemMessage(guidance)

依赖：db_session fixture 可用但 mock build_context 避免 DB 查询（C5 隔离铁律）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from langchain_core.messages import HumanMessage, SystemMessage

from app.chat.graph import build_messages_main
from app.chat.state import MainDialogueState

pytestmark = pytest.mark.asyncio


def _history(guidance: str | None = None) -> list:
    """返回 mock 历史消息列表（由 build_context 假返回）。"""
    msgs = [
        HumanMessage(content="昨天我们聊了什么？"),
        HumanMessage(content="今天心情怎么样？"),
    ]
    return msgs


async def test_build_messages_main_assembles_system_and_history():
    """正常路径：装配产物含 SystemMessage + 历史 HumanMessage。"""
    from types import SimpleNamespace

    from app.chat.context_schema import ChatContextSchema

    ctx = ChatContextSchema(
        session_id="00000000-0000-0000-0000-000000000001",
        child_user_id="00000000-0000-0000-0000-000000000002",
        child_profile={},
        age=8,
        gender="male",
        user_input="我今天很开心",
        settings=MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=MagicMock(),
    )
    state: MainDialogueState = {
        "session_id": "s1", "child_user_id": "c1", "provider": "deepseek",
        "messages": [], "audit_state": {"crisis_locked": False,
                                         "crisis_detected": False,
                                         "redline_triggered": False,
                                         "guidance": None},
        "generated_token_count": 0, "client_alive": True,
        "user_stop_requested": False, "turn_number": 1,
    }
    runtime = SimpleNamespace(context=ctx)

    with patch("app.chat.graph.build_context", return_value=_history()):
        result = await build_messages_main(state, runtime)

    msgs = result["messages"]
    # 首条应为 SystemPrompt（来自 build_system_prompt）
    assert isinstance(msgs[0], SystemMessage), "首条消息应为 SystemMessage"
    # 历史消息应完整保留
    assert any(isinstance(m, HumanMessage) and "昨天我们聊了什么" in m.content
               for m in msgs)
    assert any(isinstance(m, HumanMessage) and "今天心情怎么样" in m.content
               for m in msgs)


async def test_build_messages_main_with_guidance():
    """guidance 模式：末位 HumanMessage 前含 guidance SystemMessage。"""
    from types import SimpleNamespace

    from app.chat.context_schema import ChatContextSchema

    ctx = ChatContextSchema(
        session_id="00000000-0000-0000-0000-000000000001",
        child_user_id="00000000-0000-0000-0000-000000000002",
        child_profile={},
        age=8,
        gender="male",
        user_input="我想玩游戏",
        settings=MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=MagicMock(),
    )
    state: MainDialogueState = {
        "session_id": "s1", "child_user_id": "c1", "provider": "deepseek",
        "messages": [], "audit_state": {"crisis_locked": False,
                                         "crisis_detected": False,
                                         "redline_triggered": False,
                                         "guidance": "建议引导到户外活动"},
        "generated_token_count": 0, "client_alive": True,
        "user_stop_requested": False, "turn_number": 1,
    }
    runtime = SimpleNamespace(context=ctx)

    with patch("app.chat.graph.build_context", return_value=_history()):
        result = await build_messages_main(state, runtime)

    msgs = result["messages"]
    # 找到所有 HumanMessage 索引
    human_indices = [i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)]
    assert len(human_indices) >= 1, "至少应有 1 条 HumanMessage"

    # 末位 HumanMessage 前应有一条 SystemMessage 包含 guidance 文案
    last_human_idx = human_indices[-1]
    assert last_human_idx > 0, "末位 HumanMessage 不应是首条"
    guidance_msg = msgs[last_human_idx - 1]
    assert isinstance(guidance_msg, SystemMessage), "guidance 应包装为 SystemMessage"
    assert "建议引导到户外活动" in guidance_msg.content
