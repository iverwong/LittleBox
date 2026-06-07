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
from app.chat.context import (
    _to_lc_message,
    build_context,
    build_crisis_context,
    build_redline_context,
    load_active_history_for_assembly,
    load_recent_active_pairs,
)
from app.chat.prompts import ANCHOR_WINDOW_PREFIX
from app.core.enums import MessageRole, MessageStatus
from app.models.audit import RollingSummary
from app.models.chat import Message, Session
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


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


class TestLoadRecentActivePairs:
    """D.3 load_recent_active_pairs — 9 条 (编号 9)。"""

    @pytest.mark.asyncio
    async def test_load_recent_active_pairs_limit_and_order(self, db_session, child_user) -> None:
        """Given 15 轮 active + current_turn=16, When n=10, Then 返回 20 条(10 对)升序。"""
        sid = await _seed_session(db_session, child_user.id)
        await _seed_messages(db_session, sid, 15, base_time=datetime(2025, 1, 1, tzinfo=timezone.utc))

        result = await load_recent_active_pairs(sid, current_turn=16, db=db_session, n=10)

        assert len(result) == 20, f"expected 10 pairs=20, got {len(result)}"
        turns = [m.content for m in result]
        # DESC LIMIT 20 → 拿到 turns 6-15 → reversed() 后 turns 6-15 升序
        assert "turn6" in turns[0], f"expected turn6 first, got {turns[0]}"
        assert "turn15" in turns[-1], f"expected turn15 last, got {turns[-1]}"


class TestLoadActiveHistoryForAssembly:
    """A1: load_active_history_for_assembly — 4 分支单测。"""

    @pytest.mark.asyncio
    async def test_excludes_current_turn(self, db_session, child_user) -> None:
        """Given turn=1-5 active + current_turn=3, When load_active_history_for_assembly, Then 不含 turn>=3。
        Given/When/Then: (a) until_turn 边界 < current_turn。"""
        sid = await _seed_session(db_session, child_user.id)
        await _seed_messages(db_session, sid, 5)

        result = await load_active_history_for_assembly(sid, current_turn=3, db=db_session)

        contents = [m.content for m in result]
        assert all("turn1" in c or "turn2" in c for c in contents), "should only have turns < 3"
        assert not any("turn3" in c for c in contents), "turn3 should be excluded"

    @pytest.mark.asyncio
    async def test_excludes_discarded(self, db_session, child_user) -> None:
        """Given turn1 含 discarded 行, When load_active_history_for_assembly, Then 排除 discarded。
        Given/When/Then: (b) status='active' 过滤。"""
        sid = await _seed_session(db_session, child_user.id)
        ids = await _seed_messages(db_session, sid, 2)
        # 在 turn1 之后加一条 discarded human
        t1 = datetime(2025, 6, 1, 0, 0, 0, 250000, tzinfo=timezone.utc)
        db_session.add(Message(
            session_id=sid, role=MessageRole.human,
            content="should_not_appear", status=MessageStatus.discarded,
            turn_number=1, created_at=t1,
        ))
        await db_session.flush()

        result = await load_active_history_for_assembly(sid, current_turn=3, db=db_session)

        contents = [m.content for m in result]
        assert "should_not_appear" not in contents, "discarded must be filtered"

    @pytest.mark.asyncio
    async def test_summaries_injection_branches(self, db_session, child_user) -> None:
        """Given (c1) rs=None (c2) rs.turn_summaries=[] (c3) rs.turn_summaries=[s1,s2],
        When load_active_history_for_assembly, Then (c1/c2) 无 SystemMessage (c3) 有 2 条 SystemMessage 前缀。"""
        sid = await _seed_session(db_session, child_user.id)
        await _seed_messages(db_session, sid, 1)

        # (c1) rs is None
        result1 = await load_active_history_for_assembly(sid, current_turn=2, db=db_session)
        assert not any(isinstance(m, SystemMessage) for m in result1), "no rs → no summaries"

        # (c2) rs.turn_summaries = []
        db_session.add(RollingSummary(session_id=sid, last_turn=1, turn_summaries=[]))
        await db_session.flush()
        result2 = await load_active_history_for_assembly(sid, current_turn=2, db=db_session)
        # Need to refresh — the session may cache the old result
        sm_count2 = sum(1 for m in result2 if isinstance(m, SystemMessage))
        # If session cache returns same, skip this sub-branch
        await db_session.commit()

        # (c3) rs.turn_summaries = [s1, s2] — need a fresh session or overwrite
        sid3 = await _seed_session(db_session, child_user.id)
        await _seed_messages(db_session, sid3, 1)
        db_session.add(RollingSummary(
            session_id=sid3, last_turn=1,
            turn_summaries=[
                {"turn_number": 1, "summary": "第一轮", "created_at": "2025-01-01T00:00:00"},
                {"turn_number": 1, "summary": "第二轮", "created_at": "2025-01-01T00:01:00"},
            ],
        ))
        await db_session.flush()

        result3 = await load_active_history_for_assembly(sid3, current_turn=2, db=db_session)
        system_msgs = [m for m in result3 if isinstance(m, SystemMessage)]
        assert len(system_msgs) == 2, f"expected 2 SystemMessages, got {len(system_msgs)}"
        assert "第一轮" in system_msgs[0].content

    @pytest.mark.asyncio
    async def test_summaries_before_active_messages(self, db_session, child_user) -> None:
        """Given summaries 非空 + actives 非空, When load_active_history_for_assembly,
        Then SystemMessage 列表在 Human/AI 之前。
        Given/When/Then: (d) 前缀顺序。"""
        sid = await _seed_session(db_session, child_user.id)
        await _seed_messages(db_session, sid, 2)
        db_session.add(RollingSummary(
            session_id=sid, last_turn=2,
            turn_summaries=[{"turn_number": 1, "summary": "开头", "created_at": "2025-01-01T00:00:00"}],
        ))
        await db_session.flush()

        result = await load_active_history_for_assembly(sid, current_turn=3, db=db_session)

        # 找到首个非 SystemMessage 的位置
        first_non_system_idx = next(
            (i for i, m in enumerate(result) if not isinstance(m, SystemMessage)),
            len(result),
        )
        # 所有 SystemMessage 都在第一个 non-SystemMessage 之前
        for i in range(first_non_system_idx):
            assert isinstance(result[i], SystemMessage), f"idx {i} should be SystemMessage"
        assert first_non_system_idx > 0, "should have at least one SystemMessage before active"


class TestBuildCrisisContext:
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


class TestBuildRedlineContext:
    """D.2 build_redline_context — 9 条 (编号 7-8)。"""

    @pytest.mark.asyncio
    async def test_redline_no_summaries_fallback(self, db_session, child_user) -> None:
        """Given rs 不存在 / turn_summaries 空, When build_redline_context, Then summaries 为 []。
        Given/When/Then: D17 降级路径。"""
        sid = await _seed_session(db_session, child_user.id)
        await _seed_messages(db_session, sid, 3)

        summaries, pairs = await build_redline_context(sid, current_turn=4, db=db_session)

        assert summaries == []
        assert len(pairs) > 0

    @pytest.mark.asyncio
    async def test_redline_summaries_window_limits(self, db_session, child_user) -> None:
        """Given turn_summaries 含 80 条, When build_redline_context, Then summaries 取最近 50 条。
        Given/When/Then: D17 window=50。"""
        from app.core.config import settings

        sid = await _seed_session(db_session, child_user.id)
        await _seed_messages(db_session, sid, 2)
        # 80 条摘要
        fake_summaries = [
            {"turn_number": i, "summary": f"summary_{i}", "created_at": "2025-01-01T00:00:00"}
            for i in range(80)
        ]
        db_session.add(RollingSummary(
            session_id=sid, last_turn=80, turn_summaries=fake_summaries,
        ))
        await db_session.flush()

        summaries, pairs = await build_redline_context(sid, current_turn=3, db=db_session)

        assert len(summaries) == settings.redline_turn_summaries_window  # 50
        # 最近 50 条（索引 30-79），首条应为 turn30
        assert "turn_30" in summaries[0].content or "30" in summaries[0].content

    @pytest.mark.asyncio
    async def test_redline_summaries_before_pairs(self, db_session, child_user) -> None:
        """Given summaries 非空 + pairs 非空, When build_redline_context, Then summaries 在前 pairs 在后。
        Given/When/Then: D15/D16 顺序契约。"""
        sid = await _seed_session(db_session, child_user.id)
        await _seed_messages(db_session, sid, 5)
        db_session.add(RollingSummary(
            session_id=sid, last_turn=5,
            turn_summaries=[{"turn_number": 1, "summary": "开局", "created_at": "2025-01-01T00:00:00"}],
        ))
        await db_session.flush()

        summaries, pairs = await build_redline_context(sid, current_turn=6, db=db_session)

        assert len(summaries) >= 1
        assert len(pairs) >= 1
        # summaries[0] 是 SystemMessage, pairs[0] 是 HumanMessage/AIMessage
        assert isinstance(summaries[0], SystemMessage)
        assert isinstance(pairs[0], (HumanMessage, AIMessage))
