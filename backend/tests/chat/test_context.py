"""Tests for context helper functions (to_lc_message, load_recent_messages, build_crisis_context).

关注点覆盖（5 条硬约束 + 1 条 session_notes 护栏）：

不过，rollingsummaries-related 不被当前使用。此文件 主要测试：
- load_recent_messages
- build_crisis_context
- to_lc_message

所有测试使用 conftest.py 的 PostgreSQL test-db fixtures（savepoint 隔离）。
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.domain.chat.context import (
    to_lc_message,
    build_crisis_context,
    load_recent_messages,
)
from app.domain.chat.prompts import ANCHOR_WINDOW_PREFIX
from app.core.enums import MessageRole, MessageStatus
from app.domain.audit.models import RollingSummary
from app.domain.chat.models import Message, Session
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


async def _seed_session(db_session, child_user_id: uuid.UUID) -> uuid.UUID:
    """Create a session row (Message needs session_id FK)."""
    sid = uuid.uuid4()
    s = Session(id=sid, child_user_id=child_user_id, title="test")
    db_session.add(s)
    await db_session.flush()
    return sid




# ---- _to_lc_message unit ----


def test_to_lc_message_human() -> None:
    """_to_lc_message: role=human → HumanMessage"""

    class FakeRow:
        role = MessageRole.human
        content = "hello"

    msg = to_lc_message(FakeRow())
    assert msg.__class__.__name__ == "HumanMessage"
    assert msg.content == "hello"


def test_to_lc_message_ai() -> None:
    """_to_lc_message: role=ai → AIMessage"""

    class FakeRow:
        role = MessageRole.ai
        content = "assistant reply"

    msg = to_lc_message(FakeRow())
    assert msg.__class__.__name__ == "AIMessage"
    assert msg.content == "assistant reply"


# ---------------------------------------------------------------------------
# M9 Step 5 — 三级干预上下文装配函数（§D）
# ---------------------------------------------------------------------------


async def _seed_messages(
    db_session,
    sid: uuid.UUID,
    pairs: int,
    *,
    turn_offset: int = 1,
    base_time=None,
) -> list[uuid.UUID]:
    """Helper: create N complete human+ai pairs with sequential turn_number.

    Returns [human_id, ai_id, human_id, ai_id, ...].
    """
    if base_time is None:
        base_time = datetime(2025, 6, 1, tzinfo=timezone.utc)
    ids = []
    for i in range(pairs):
        t = turn_offset + i
        for role, content_suffix in [("human", "h"), ("ai", "a")]:
            mid = uuid.uuid4()
            ids.append(mid)
            db_session.add(
                Message(
                    id=mid,
                    session_id=sid,
                    role=MessageRole(role),
                    content=f"turn{t}_{content_suffix}{t}",
                    status=MessageStatus.active,
                    turn_number=t,
                    created_at=base_time + timedelta(seconds=i * 2 + (0 if role == "human" else 1)),
                )
            )
    return ids


class TestLoadRecentActiveMessages:
    """D.1 build_crisis_context — 9 条 (编号 1-6)。"""

    @pytest.mark.asyncio
    async def test_anchor_window_bypasses_status_filter(self, db_session, child_user) -> None:
        """Given anchor 前有 discarded+compressed 行, When build_crisis_context, Then anchor_window 含它们。
        Given/When/Then: A1 要求绕 status 过滤。"""
        sid = await _seed_session(db_session, child_user.id)
        # 只用 2 对 active（4 条），窗口 2N=10 足够容纳额外行
        ids = await _seed_messages(db_session, sid, 2)
        # 在 turn2_ai(索引 3, 时间 3s)之前塞 discarded+compressed
        non_active_time = datetime(2025, 6, 1, 0, 0, 0, 500000, tzinfo=timezone.utc)
        db_session.add(Message(
            session_id=sid, role=MessageRole.human,
            content="discarded_row", status=MessageStatus.discarded,
            turn_number=1, created_at=non_active_time,
        ))
        db_session.add(Message(
            session_id=sid, role=MessageRole.summary,
            content="compressed_row", status=MessageStatus.compressed,
            turn_number=0, created_at=non_active_time + timedelta(milliseconds=1),
        ))
        anchor_id = ids[-1]  # turn2_ai（索引 3, 时间 3s）
        await db_session.flush()

        anchor_sys, after = await build_crisis_context(sid, db_session, anchor_id)

        # anchor_window(2N=10)远大于 6 条, discarded+compressed 都在窗口内
        assert "discarded_row" in anchor_sys.content
        assert "compressed_row" in anchor_sys.content

    @pytest.mark.asyncio
    async def test_after_anchor_filters_non_active(self, db_session, child_user) -> None:
        """Given anchor 后有 discarded 行, When build_crisis_context, Then after_anchor 排除它。
        Given/When/Then: A2 after_anchor 仅 active。"""
        sid = await _seed_session(db_session, child_user.id)
        ids = await _seed_messages(db_session, sid, 3)  # turns 1-3, anchor=turn3_ai
        anchor_id = ids[-1]
        # anchor 后加一条 active + 一条 discarded
        later = datetime(2025, 6, 1, 0, 1, 0, tzinfo=timezone.utc)
        db_session.add(Message(
            session_id=sid, role=MessageRole.human,
            content="after_active", status=MessageStatus.active,
            turn_number=4, created_at=later,
        ))
        db_session.add(Message(
            session_id=sid, role=MessageRole.human,
            content="after_discarded", status=MessageStatus.discarded,
            turn_number=4, created_at=later + timedelta(seconds=1),
        ))
        await db_session.flush()

        _, after = await build_crisis_context(sid, db_session, anchor_id)

        contents = [m.content for m in after]
        assert "after_active" in contents
        assert "after_discarded" not in contents

    @pytest.mark.asyncio
    async def test_first_turn_crisis_after_anchor_empty(self, db_session, child_user) -> None:
        """Given 首轮 crisis(anchor = 本轮唯一 ai), When build_crisis_context, Then after_anchor 空。
        Given/When/Then: 首轮非粘性，anchor 后无行。"""
        sid = await _seed_session(db_session, child_user.id)
        ids = await _seed_messages(db_session, sid, 1)  # 仅 1 对
        anchor_id = ids[-1]  # turn1_ai
        await db_session.flush()

        _, after = await build_crisis_context(sid, db_session, anchor_id)

        assert after == []

    @pytest.mark.asyncio
    async def test_crisis_locked_sticky_after_anchor_all_active(self, db_session, child_user) -> None:
        """Given crisis_locked(anchor=turn3) + session 到 turn15, When build_crisis_context, Then after_anchor 含 turn4-15。
        Given/When/Then: D2 粘性场景跨多轮。"""
        sid = await _seed_session(db_session, child_user.id)
        ids = await _seed_messages(db_session, sid, 15)  # turns 1-15, ids=[h1,a1,h2,a2,...,h15,a15]
        anchor_id = ids[5]  # turn3_ai = index 5
        await db_session.flush()

        _, after = await build_crisis_context(sid, db_session, anchor_id)

        assert len(after) == (15 - 3) * 2, f"expected {24} rows after turn3, got {len(after)}"
        # 第一条应为 turn4_human
        assert "turn4_h" in after[0].content

    @pytest.mark.asyncio
    async def test_anchor_not_found_raises_value_error(self, db_session, child_user) -> None:
        """Given 不存在的 UUID, When build_crisis_context, Then 抛 ValueError。
        Given/When/Then: A1 兜底。"""
        sid = await _seed_session(db_session, child_user.id)
        fake_id = uuid.uuid4()

        with pytest.raises(ValueError, match="crisis anchor not found"):
            await build_crisis_context(sid, db_session, fake_id)

    @pytest.mark.asyncio
    async def test_anchor_system_format(self, db_session, child_user) -> None:
        """Given valid anchor, When build_crisis_context, Then anchor_system.content 以 [anchor 窗口] 开头并逐行拼接。
        Given/When/Then: A6 格式断言。"""
        sid = await _seed_session(db_session, child_user.id)
        ids = await _seed_messages(db_session, sid, 2)
        anchor_id = ids[-1]  # turn2_ai

        anchor_sys, _ = await build_crisis_context(sid, db_session, anchor_id)

        assert anchor_sys.content.startswith(ANCHOR_WINDOW_PREFIX)
        lines = anchor_sys.content.split("\n")
        assert len(lines) >= 5  # 标题 + 4 行
        # 确保逐行 role: content 格式
        assert ": " in lines[1]  # "human: turn1_h1"
