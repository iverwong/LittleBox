"""crisis build_messages simple mock test."""
from __future__ import annotations

import pytest
from langchain_core.messages import SystemMessage

pytestmark = pytest.mark.asyncio


async def test_crisis_path_still_works():
    """Smoke test: crisis module still importable and crisis system prompt works."""
    from app.domain.chat.prompts import build_crisis_system_prompt
    from tests.conftest import make_child_profile_snapshot
    msg = build_crisis_system_prompt(
        make_child_profile_snapshot(age=10, gender="male"),
        crisis_topic="test",
        crisis_turn_dialogue="<turn>test</turn>",
        pre_crisis_turn_dialogue="<turn>pre</turn>",
    )
    assert isinstance(msg, SystemMessage)
    assert "test" in msg.content
