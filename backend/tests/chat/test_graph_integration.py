"""5-path 路由 + 装配集成验证（M9 Step 10 §G.1）。

测试策略（A1-A3 修订稿）：不经过 load_audit_state / call_*_llm，
直接构造 audit_state + 节点级 build_messages_* 调用，验证路由方向和装配正确性。

覆盖：
  5 path: main / crisis(触发) / crisis(粘性) / redline / guidance
  3 cascade: ready 信号驱动 route_by_risk 的 crisis/redline/guidance 分支

Given/When/Then docstring 格式。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import text

from app.chat.context_schema import ChatContextSchema
from app.chat.graph import (
    build_messages_crisis,
    build_messages_main,
    build_messages_redline,
    load_audit_state,
    route_by_risk,
)
from app.chat.state import MainDialogueState
from app.models.accounts import Family, User
from app.models.chat import Message
from app.core.enums import MessageRole, MessageStatus, UserRole

pytestmark = [
    pytest.mark.asyncio,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings(**overrides: dict) -> SimpleNamespace:
    """Return minimal settings mock with crisis/redline tuning params."""
    defaults = dict(
        main_provider="deepseek",
        deepseek_api_key="",
        deepseek_base_url="https://api.deepseek.com/v1",
        deepseek_model="deepseek-v4-flash",
        main_thinking_enabled=True,
        main_reasoning_effort="max",
        bailian_api_key="",
        bailian_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        bailian_model="deepseek-v4-flash",
        audit_thinking_enabled=True,
        audit_reasoning_effort="max",
        audit_provider="deepseek",
        llm_request_timeout_seconds=60.0,
        enable_fallback=False,
        fallback_provider=None,
        audit_redis_ttl_seconds=86400,
        audit_wait_timeout_seconds=30,
        crisis_context_recent_turns=5,
        redline_context_recent_turns=10,
        redline_turn_summaries_window=50,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


_ALL_FALSE_AUDIT = {
    "crisis_locked": False,
    "crisis_detected": False,
    "redline_triggered": False,
    "guidance": None,
    "target_message_id": None,
}


def _make_state(
    *,
    turn_number: int = 2,
    audit_state: dict | None = None,
    messages: list | None = None,
) -> MainDialogueState:
    return {
        "messages": messages or [],
        "audit_state": audit_state or dict(_ALL_FALSE_AUDIT),
        "generated_token_count": 0,
        "client_alive": True,
        "user_stop_requested": False,
        "turn_number": turn_number,
    }


def _make_runtime(
    *,
    session_id: str = "00000000-0000-0000-0000-000000000001",
    user_input: str = "测试输入",
    age: int = 10,
    gender: str | None = "male",
    settings=None,
    db_session_factory=None,
) -> SimpleNamespace:
    """构造最小 Runtime[ChatContextSchema]，db_session_factory 可传真实 factory。"""
    ctx = ChatContextSchema(
        session_id=session_id,
        child_user_id="child-1",
        child_profile={},
        age=age,
        gender=gender,
        user_input=user_input,
        settings=settings or _mock_settings(),
        db_session_factory=db_session_factory or MagicMock(),
        audit_redis=MagicMock(),
    )
    return SimpleNamespace(context=ctx)


def _wrap_db_factory(db_session):
    """把 db_session fixture 包装成 async context manager 工厂。"""
    import contextlib

    @contextlib.asynccontextmanager
    async def _f():
        yield db_session

    return _f


# ---------------------------------------------------------------------------
# §G.1：5 path — 路由方向 + 装配验证
# ---------------------------------------------------------------------------


class TestMainPath:
    """main 路径：all-False → route_by_risk=main → build_messages_main 装配。"""

    async def test_route_by_risk(self):
        """Given: all-False audit_state, When route_by_risk, Then returns 'main'."""
        state = _make_state(audit_state=dict(_ALL_FALSE_AUDIT))
        assert route_by_risk(state) == "main"

    async def test_assembly(self):
        """Given: main path, When build_messages_main, Then system + history + plain human."""
        state = _make_state(turn_number=1, audit_state=dict(_ALL_FALSE_AUDIT))
        runtime = _make_runtime(user_input="你好")
        fake_history = [HumanMessage(content="历史消息")]

        with patch(
            "app.chat.graph.load_active_history_for_assembly",
            return_value=fake_history,
        ):
            result = await build_messages_main(state, runtime)

        msgs = result["messages"]
        assert isinstance(msgs[0], SystemMessage), "首条应为 system prompt"
        assert msgs[1] is fake_history[0], "历史消息应在第 2 位"
        assert isinstance(msgs[-1], HumanMessage), "末位应为 human"
        assert msgs[-1].content == "你好", "guidance=None 应透传 user_input"


class TestCrisisTriggerPath:
    """crisis（触发，crisis_detected=True）：route= crisis → build_messages_crisis 装配。"""

    async def test_route_by_risk(self):
        """Given: crisis_detected=True, When route_by_risk, Then returns 'crisis'."""
        state = _make_state(audit_state={
            "crisis_locked": False, "crisis_detected": True,
            "redline_triggered": False, "guidance": None,
            "target_message_id": uuid.uuid4(),
        })
        assert route_by_risk(state) == "crisis"

    async def test_assembly_with_real_db(self, db_session):
        """Given: crisis_detected=True + seed DB, When build_messages_crisis,
        Then 装配 [crisis_system, anchor_system, *after_anchor, reentry_wrapper]。
        """
        # ---- seed ----
        sid = uuid.uuid4()
        child_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        fam = Family()
        db_session.add(fam)
        await db_session.flush()
        child = User(id=child_id, family_id=fam.id, role=UserRole.child, phone="cr", is_active=True)
        db_session.add(child)
        await db_session.flush()
        await db_session.execute(
            text("INSERT INTO sessions (id, child_user_id, status, last_active_at, created_at, needs_compression) "
                 "VALUES (:id, :cid, 'active', :now, :now, false)"),
            {"id": sid, "cid": child_id, "now": now},
        )

        # pre-anchor message (discarded — anchor_window should still include)
        pre = Message(
            session_id=sid, role=MessageRole.human, content="被丢弃的内容",
            status=MessageStatus.discarded, turn_number=1,
            created_at=now - timedelta(minutes=5),
        )
        db_session.add(pre)

        # anchor message (target_message_id — the crisis-triggering AI msg)
        anchor = Message(
            session_id=sid, role=MessageRole.ai, content="危机触发回复",
            status=MessageStatus.active, turn_number=2,
            created_at=now - timedelta(minutes=2),
        )
        db_session.add(anchor)
        await db_session.flush()
        target_mid = anchor.id
        # anchor.created_at 必须落值后再用于比较 — create_at 由 server_default 填充
        assert anchor.created_at is not None, "anchor.created_at 须在 flush 后落值"

        # post-anchor message (active — should appear in after_anchor)
        post = Message(
            session_id=sid, role=MessageRole.human, content="之后的内容",
            status=MessageStatus.active, turn_number=3,
            created_at=now - timedelta(minutes=1),
        )
        db_session.add(post)
        await db_session.flush()

        # ---- invoke ----
        state = _make_state(turn_number=3, audit_state={
            "crisis_locked": False, "crisis_detected": True,
            "redline_triggered": False, "guidance": None,
            "target_message_id": target_mid,
        })
        runtime = _make_runtime(
            session_id=str(sid), user_input="求助消息",
            db_session_factory=_wrap_db_factory(db_session),
        )
        result = await build_messages_crisis(state, runtime)
        msgs = result["messages"]

        # ---- assert assembly ----
        assert len(msgs) >= 4, (
            f"应含 [crisis_system, anchor_system, *after_anchor, reentry_wrapper]，"
            f"实际 {len(msgs)} 条"
        )
        assert isinstance(msgs[0], SystemMessage), "msgs[0] 应为 crisis system prompt"
        assert isinstance(msgs[1], SystemMessage), "msgs[1] 应为 anchor_window"
        assert "[anchor 窗口]" in msgs[1].content, "anchor_window 应含标识头"
        # after_anchor: 至少 post 消息
        assert any("之后的内容" in m.content for m in msgs[2:-1] if isinstance(m, HumanMessage))
        # 末位 reentry wrapper
        assert isinstance(msgs[-1], HumanMessage), "末位应为 HumanMessage(reentry)"
        assert "求助消息" in msgs[-1].content, "reentry wrapper 应包含 user_input"


class TestCrisisStickyPath:
    """crisis（粘性，crisis_locked=True）：route= crisis → build_messages_crisis。"""

    async def test_route_by_risk(self):
        """Given: crisis_locked=True + crisis_detected=False + redline_triggered=True,
        When route_by_risk, Then returns 'crisis'（priority ① > ③）。
        """
        state = _make_state(audit_state={
            "crisis_locked": True, "crisis_detected": False,
            "redline_triggered": True, "guidance": "引导",
            "target_message_id": uuid.uuid4(),
        })
        assert route_by_risk(state) == "crisis"

    async def test_assembly(self):
        """Given: crisis_locked=True, When build_messages_crisis, Then 装配顺序同触发路径。"""
        # 装配函数本身与触发路径相同（区别仅为 target_message_id 来源不同 — 测试已提供）
        # 此处验证 build_messages_crisis 在 crisis_locked state 下不抛异常
        state = _make_state(turn_number=3, audit_state={
            "crisis_locked": True, "crisis_detected": False,
            "redline_triggered": False, "guidance": None,
            "target_message_id": uuid.uuid4(),
        })
        runtime = _make_runtime(db_session_factory=MagicMock())

        # build_crisis_context 会查真库 — mock 掉
        fake_anchor = SystemMessage(content="[anchor 窗口]\nmock")
        with (
            patch("app.chat.graph.build_crisis_context",
                  return_value=(fake_anchor, [HumanMessage(content="mock after")])),
            patch("app.chat.graph.build_crisis_system_prompt",
                  return_value=SystemMessage(content="[crisis system]")),
        ):
            result = await build_messages_crisis(state, runtime)

        msgs = result["messages"]
        assert msgs[0].content == "[crisis system]"
        assert msgs[1] is fake_anchor
        assert isinstance(msgs[-1], HumanMessage)
        assert "测试输入" in msgs[-1].content


class TestRedlinePath:
    """redline 路径：redline_triggered=True → route=redline → build_messages_redline。"""

    async def test_route_by_risk(self):
        """Given: redline_triggered=True + crisis=F + guidance=非空,
        When route_by_risk, Then returns 'redline'（priority ③ > ④）。
        """
        state = _make_state(audit_state={
            "crisis_locked": False, "crisis_detected": False,
            "redline_triggered": True, "guidance": "忽略的红线",
            "target_message_id": None,
        })
        assert route_by_risk(state) == "redline"

    async def test_assembly_with_real_db(self, db_session):
        """Given: redline state + seed DB, When build_messages_redline,
        Then 装配 [redline_system, *summaries, *recent_pairs, reentry_wrapper]。
        """
        # ---- seed ----
        sid = uuid.uuid4()
        child_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        fam = Family()
        db_session.add(fam)
        await db_session.flush()
        child = User(id=child_id, family_id=fam.id, role=UserRole.child, phone="rl", is_active=True)
        db_session.add(child)
        await db_session.flush()
        await db_session.execute(
            text("INSERT INTO sessions (id, child_user_id, status, last_active_at, created_at, needs_compression) "
                 "VALUES (:id, :cid, 'active', :now, :now, false)"),
            {"id": sid, "cid": child_id, "now": now},
        )

        # rolling_summaries（含 turn_summaries）
        from app.models.audit import RollingSummary
        rs = RollingSummary(
            session_id=sid, last_turn=2,
            turn_summaries=[
                {"turn_number": 1, "summary": "第一轮摘要", "created_at": now.isoformat()},
            ],
            session_notes="注意社交互动",
        )
        db_session.add(rs)

        # active pairs（turn_number < current_turn=3，status='active'）
        for i in range(1, 3):
            m_h = Message(
                session_id=sid, role=MessageRole.human,
                content=f"用户第{i}轮", status=MessageStatus.active,
                turn_number=i, created_at=now - timedelta(minutes=10 - i),
            )
            db_session.add(m_h)
            m_a = Message(
                session_id=sid, role=MessageRole.ai,
                content=f"AI第{i}轮回复", status=MessageStatus.active,
                turn_number=i, created_at=now - timedelta(minutes=9 - i),
            )
            db_session.add(m_a)
        await db_session.flush()

        # ---- invoke ----
        current_turn = 3
        state = _make_state(turn_number=current_turn, audit_state={
            "crisis_locked": False, "crisis_detected": False,
            "redline_triggered": True, "guidance": None,
            "target_message_id": None,
        })
        runtime = _make_runtime(
            session_id=str(sid), user_input="红线测试",
            db_session_factory=_wrap_db_factory(db_session),
            settings=_mock_settings(),
        )
        result = await build_messages_redline(state, runtime)
        msgs = result["messages"]

        # ---- assert ----
        assert isinstance(msgs[0], SystemMessage), "msgs[0] 应为 redline system prompt"
        # summaries 在 msgs[1:] 中
        summaries_found = [m for m in msgs if isinstance(m, SystemMessage) and m is not msgs[0]]
        assert any("第一轮摘要" in m.content for m in summaries_found), "turn_summary 应注入"

        # recent pairs（active + turn_number < current_turn）
        assert any("用户第1轮" in m.content for m in msgs), "第1轮 human 应在 recent pairs"
        assert not any("turn_number=3" in getattr(m, "content", "") for m in msgs), (
            "不应含本轮（current_turn）的消息"
        )

        # 末位 reentry wrapper
        assert isinstance(msgs[-1], HumanMessage), "末位应为 reentry wrapper"
        assert "红线测试" in msgs[-1].content


class TestGuidancePath:
    """guidance 路径：guidance=非空 → route=guidance → build_messages_main 装配。"""

    async def test_route_by_risk(self):
        """Given: guidance 非空 + crisis/redline=F,
        When route_by_risk, Then returns 'guidance'（priority ④）。
        """
        state = _make_state(audit_state={
            "crisis_locked": False, "crisis_detected": False,
            "redline_triggered": False, "guidance": "建议鼓励运动",
            "target_message_id": None,
        })
        assert route_by_risk(state) == "guidance"

    async def test_assembly(self):
        """Given: guidance 非空, When build_messages_main, Then 末位 HumanMessage 含 STUB 标记。"""
        state = _make_state(turn_number=1, audit_state={
            "crisis_locked": False, "crisis_detected": False,
            "redline_triggered": False, "guidance": "鼓励运动",
            "target_message_id": None,
        })
        runtime = _make_runtime(user_input="我不想动", settings=_mock_settings())
        fake_history = [HumanMessage(content="之前的消息")]

        with patch(
            "app.chat.graph.load_active_history_for_assembly",
            return_value=fake_history,
        ):
            result = await build_messages_main(state, runtime)

        msgs = result["messages"]
        assert isinstance(msgs[0], SystemMessage), "首条应为 system prompt"
        last = msgs[-1]
        assert isinstance(last, HumanMessage), "末位应为 HumanMessage"
        assert "TODO(prompts-content)" in last.content, "guidance 非空时末位应含 STUB 标记"
        assert "我不想动" in last.content, "user_input 应在 wrapper 内"
        assert "鼓励运动" in last.content, "guidance 应在 wrapper 内"


# ---------------------------------------------------------------------------
# load_audit_state → route_by_risk cascade（A1 补充：上游信号驱动验证）
# ---------------------------------------------------------------------------


class TestAuditStateCascade:
    """验证 load_audit_state 输出的 audit_state 能正确驱动 route_by_risk。

    绕开 Redis poll：直接 mock AuditSignalsManager.poll_wait 的返回值。
    """

    @pytest.mark.asyncio
    async def test_ready_crisis_routes_crisis(self):
        """Given: poll_wait ready + signals.crisis_detected=True,
        When load_audit_state, Then audit_state → route_by_risk → 'crisis'。
        """
        result = await self._do_cascade("crisis_detected", True)
        audit = result["audit_state"]
        assert audit["crisis_detected"] is True
        state = _make_state(audit_state=audit)
        assert route_by_risk(state) == "crisis"

    @pytest.mark.asyncio
    async def test_ready_redline_routes_redline(self):
        """Given: poll_wait ready + signals.redline_triggered=True,
        When load_audit_state, Then audit_state → route_by_risk → 'redline'。
        """
        result = await self._do_cascade("redline_triggered", True)
        audit = result["audit_state"]
        assert audit["redline_triggered"] is True
        state = _make_state(audit_state=audit)
        assert route_by_risk(state) == "redline"

    @pytest.mark.asyncio
    async def test_ready_guidance_routes_guidance(self):
        """Given: poll_wait ready + signals.guidance=非空,
        When load_audit_state, Then audit_state → route_by_risk → 'guidance'。
        """
        result = await self._do_cascade("guidance", "测试引导")
        audit = result["audit_state"]
        assert audit["guidance"] == "测试引导"
        state = _make_state(audit_state=audit)
        assert route_by_risk(state) == "guidance"

    async def _do_cascade(self, field: str, value) -> dict:
        """共享逻辑：mock poll_wait ready → load_audit_state"""
        from unittest.mock import AsyncMock, patch

        from app.chat.graph import _pg_crisis_fallback
        from app.domain.audit.schemas import AuditDimensionScores, AuditOutputSchema
        from app.domain.audit.signals import AuditWaitResult

        # AuditOutputSchema validation: guidance 必填 string；crisis_topic/redline_detail
        # 在对应 trigger=True 时必须非空。
        _crisis_detected = value if field == "crisis_detected" else False
        _redline_triggered = value if field == "redline_triggered" else False
        _guidance = value if field == "guidance" else ""
        signals = AuditOutputSchema(
            dimension_scores=AuditDimensionScores(),
            crisis_detected=_crisis_detected,
            crisis_topic="crisis topic" if _crisis_detected else None,
            redline_triggered=_redline_triggered,
            redline_detail="redline detail" if _redline_triggered else None,
            guidance_injection=_guidance,
            turn_summary="测试",
        )

        async def _mock_poll(sid, expected_turn, timeout=None):
            return AuditWaitResult(kind="ready", signals=signals)

        async def _mock_pg(ctx):
            return {"crisis_locked": False, "target_message_id": None}

        state = _make_state(turn_number=2)
        runtime = _make_runtime()

        with (
            patch(
                "app.chat.graph.AuditSignalsManager",
                return_value=AsyncMock(poll_wait=_mock_poll),
            ),
            patch("app.chat.graph._pg_crisis_fallback", side_effect=_mock_pg),
        ):
            return await load_audit_state(state, runtime)
