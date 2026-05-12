"""Tests for context.build_context.

关注点覆盖（5 条硬约束 + 1 条 session_notes 护栏）：
1. empty session → []
2. 25 条 total（5 discarded + 20 active）→ 仅返回 20 active，无 LIMIT 截断
3. discarded 行被过滤
4. rolling_summaries 三态 fallback:
   (a) 表无该 session 行 → fallback
   (b) 行存在 + turn_summaries IS NULL → fallback
   (c) 行存在 + turn_summaries = [] → fallback
5. M8 fallthrough: turn_summaries 非空 → 注入 SystemMessage 在 list 首位
6. session_notes 不泄露进主 LLM（硬断言）

所有测试使用 conftest.py 的 PostgreSQL test-db fixtures（savepoint 隔离）。
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.chat.context import _to_lc_message, build_context
from app.models.audit import RollingSummary
from app.models.chat import Message, Session
from app.models.enums import MessageRole, MessageStatus


async def _seed_session(db_session, child_user_id: uuid.UUID) -> uuid.UUID:
    """Create a session row (Message needs session_id FK)."""
    sid = uuid.uuid4()
    s = Session(id=sid, child_user_id=child_user_id, title="test")
    db_session.add(s)
    await db_session.flush()
    return sid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_session_returns_empty_list(db_session, child_user) -> None:
    """空 session → build_context 返回 []（不抛异常）。"""
    sid = await _seed_session(db_session, child_user.id)
    result = await build_context(sid, db_session)
    assert result == []


@pytest.mark.asyncio
async def test_window_20_messages_ascending(db_session, child_user) -> None:
    """25 条 total（5 discarded + 20 active）→ 仅返回 20 active，时间正序，无 LIMIT 截断。"""
    sid = await _seed_session(db_session, child_user.id)

    base = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(25):
        msg = Message(
            session_id=sid,
            role=MessageRole.human if i % 2 == 0 else MessageRole.ai,
            content=f"msg-{i:02d}",
            status=MessageStatus.discarded if i < 5 else MessageStatus.active,
            created_at=base,  # same ts; DB ordering within same ts is insertion-order dependent
        )
        db_session.add(msg)
    await db_session.flush()

    result = await build_context(sid, db_session)

    # Should have 20 (25-5 discarded)
    assert len(result) == 20
    # Key invariant: no discarded message (msg-00..msg-04) should be in result
    discarded = {f"msg-{i:02d}" for i in range(5)}
    result_contents = {str(m.content) for m in result}
    assert not discarded & result_contents, (
        f"discarded messages leaked: {discarded & result_contents}"
    )
    for m in result:
        assert m.__class__.__name__ in ("HumanMessage", "AIMessage")


@pytest.mark.asyncio
async def test_discarded_filtered(db_session, child_user) -> None:
    """只有 discarded 消息的 session → build_context 返回 []。"""
    sid = await _seed_session(db_session, child_user.id)

    for _ in range(3):
        msg = Message(
            session_id=sid,
            role=MessageRole.human,
            content="discarded-msg",
            status=MessageStatus.discarded,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        db_session.add(msg)
    await db_session.flush()

    result = await build_context(sid, db_session)
    assert result == []


# ---- rolling_summaries fallback (三态) ----


@pytest.mark.asyncio
async def test_summaries_fallback_no_row(db_session, child_user) -> None:
    """(a) rolling_summaries 表无该 session 行 → fallback，return messages only."""
    sid = await _seed_session(db_session, child_user.id)

    msg = Message(
        session_id=sid,
        role=MessageRole.human,
        content="hello",
        status=MessageStatus.active,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(msg)
    await db_session.flush()

    result = await build_context(sid, db_session)

    assert len(result) == 1
    assert result[0].content == "hello"
    assert not any(m.__class__.__name__ == "SystemMessage" for m in result)


@pytest.mark.asyncio
async def test_summaries_fallback_null_field(db_session, child_user) -> None:
    """(b) 行存在 + turn_summaries IS NULL → fallback。"""
    sid = await _seed_session(db_session, child_user.id)

    msg = Message(
        session_id=sid,
        role=MessageRole.human,
        content="hello",
        status=MessageStatus.active,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(msg)
    db_session.add(RollingSummary(session_id=sid, last_turn=0, turn_summaries=None))
    await db_session.flush()

    result = await build_context(sid, db_session)

    assert len(result) == 1
    assert not any(m.__class__.__name__ == "SystemMessage" for m in result)


@pytest.mark.asyncio
async def test_summaries_fallback_empty_list(db_session, child_user) -> None:
    """(c) 行存在 + turn_summaries = [] → fallback（空列表 falsy）。"""
    sid = await _seed_session(db_session, child_user.id)

    msg = Message(
        session_id=sid,
        role=MessageRole.human,
        content="hello",
        status=MessageStatus.active,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(msg)
    db_session.add(RollingSummary(session_id=sid, last_turn=0, turn_summaries=[]))
    await db_session.flush()

    result = await build_context(sid, db_session)

    assert len(result) == 1
    assert not any(m.__class__.__name__ == "SystemMessage" for m in result)


@pytest.mark.asyncio
async def test_m8_fallthrough_summary_injection(db_session, child_user) -> None:
    """(d) M8 落库后 turn_summaries 非空 → SystemMessage 注入在 list 首位。"""
    sid = await _seed_session(db_session, child_user.id)

    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    db_session.add(
        Message(
            session_id=sid,
            role=MessageRole.human,
            content="hello",
            status=MessageStatus.active,
            created_at=base_time,
        )
    )
    db_session.add(
        Message(
            session_id=sid,
            role=MessageRole.ai,
            content="hi there",
            status=MessageStatus.active,
            created_at=base_time + timedelta(seconds=1),
        )
    )
    summaries = [
        {"turn": 1, "summary": "child asked about homework"},
        {"turn": 2, "summary": "child asked about friends"},
    ]
    db_session.add(RollingSummary(session_id=sid, last_turn=2, turn_summaries=summaries))
    await db_session.flush()

    result = await build_context(sid, db_session)

    assert len(result) == 3
    sys_msg = result[0]
    assert sys_msg.__class__.__name__ == "SystemMessage"
    assert "Turn 1: child asked about homework" in sys_msg.content
    assert "Turn 2: child asked about friends" in sys_msg.content
    assert result[1].content == "hello"
    assert result[2].content == "hi there"


@pytest.mark.asyncio
async def test_session_notes_not_injected(db_session, child_user) -> None:
    """session_notes 不得泄露进主 LLM（硬断言）。

    架构基线 §四"字段消费分工"：session_notes 仅审查自身跨轮 + 日终专家消费，
    不注入主 LLM（避免风控判断泄漏）。
    """
    sid = await _seed_session(db_session, child_user.id)

    sentinel = "SESSION_NOTES_SENTINEL_XYZ"
    db_session.add(
        Message(
            session_id=sid,
            role=MessageRole.human,
            content="hello",
            status=MessageStatus.active,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    db_session.add(
        RollingSummary(
            session_id=sid,
            last_turn=1,
            turn_summaries=[{"turn": 1, "summary": "normal summary"}],
            session_notes=sentinel,
        )
    )
    await db_session.flush()

    result = await build_context(sid, db_session)

    for m in result:
        if m.__class__.__name__ == "SystemMessage":
            assert sentinel not in m.content, "session_notes leaked into LLM input"


# ---- _to_lc_message unit ----


def test_to_lc_message_human() -> None:
    """_to_lc_message: role=human → HumanMessage"""

    class FakeRow:
        role = MessageRole.human
        content = "hello"

    msg = _to_lc_message(FakeRow())
    assert msg.__class__.__name__ == "HumanMessage"
    assert msg.content == "hello"


def test_to_lc_message_ai() -> None:
    """_to_lc_message: role=ai → AIMessage"""

    class FakeRow:
        role = MessageRole.ai
        content = "assistant reply"

    msg = _to_lc_message(FakeRow())
    assert msg.__class__.__name__ == "AIMessage"
    assert msg.content == "assistant reply"
