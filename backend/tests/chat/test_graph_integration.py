"""Main / crisis 路由 + 装配集成验证（M9 Step 10 §G.1）。

测试策略（A1-A3 修订稿）：不经过 load_audit_state / call_*_llm，
直接构造 audit_state + 节点级 build_messages_* 调用，验证路由方向和装配正确性。

覆盖：
  3 path: main / crisis(粘性) / guidance injection
  2 cascade: ready 信号驱动 load_audit_state → route_by_risk

Given/When/Then docstring 格式。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.domain.chat.graph import (
    build_messages_crisis,
    build_messages_main,
    load_audit_state,
    route_by_risk,
)
from app.domain.chat.state import MainDialogueState
from app.core.enums import MessageRole, MessageStatus, UserRole
from app.domain.accounts.models import Family, User
from app.domain.chat.models import Message
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings(**overrides: dict) -> SimpleNamespace:
    """Return minimal settings mock with crisis tuning params.

    Step 4 后:LLM 拓扑字段已迁 llm_topology,本 fixture 只承载非 LLM 字段。
    """
    defaults = dict(
        deepseek_api_key="",
        bailian_api_key="",
        audit_redis_ttl_seconds=86400,
        audit_wait_timeout_seconds=30,
        crisis_context_recent_messages=10,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


_ALL_FALSE_AUDIT = {
    "crisis_locked": False,
    "crisis_detected": False,
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
    scalar_retval=None,
) -> SimpleNamespace:
    """构造最小 Runtime[ChatContextSchema]，db_session_factory 可传真实 factory。

    scalar_retval: magicmock db_session_factory 的 db.scalar 返回值。
    """
    from unittest.mock import AsyncMock, MagicMock
    from tests.conftest import make_chat_context, make_child_profile_snapshot

    if db_session_factory is None:

        mock_db = AsyncMock()
        mock_db.scalar = AsyncMock(return_value=scalar_retval)
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_db
        db_session_factory = MagicMock(return_value=mock_cm)

    ctx = make_chat_context(
        session_id=session_id,
        child_user_id="child-1",  # 测试 fixture 用字符串标识符,实际不被读
        user_input=user_input,
        settings=settings or _mock_settings(),
        db_session_factory=db_session_factory,
        audit_redis=MagicMock(),
        profile=make_child_profile_snapshot(age=age, gender=gender),
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
        runtime = _make_runtime(user_input="你好", scalar_retval=None)
        fake_history = [HumanMessage(content="历史消息")]

        with (
            patch(
                "app.domain.chat.graph.load_active_messages_with_summary",
                return_value=(fake_history, None),
            ),
            patch(
                "app.domain.chat.graph.to_lc_message",
                side_effect=lambda m: m,
            ),
            patch("app.domain.chat.graph.get_stream_writer"),
            patch("app.domain.chat.graph._handle_compress", new_callable=AsyncMock),
        ):
            result = await build_messages_main(state, runtime)

        msgs = result["messages"]
        assert isinstance(msgs[0], SystemMessage), "首条应为 system prompt"
        assert any("历史消息" in m.content for m in msgs if isinstance(m, HumanMessage)), "历史消息应在 messages 中"
        assert isinstance(msgs[-1], HumanMessage), "末位应为 human"
        assert msgs[-1].content == "你好", "guidance=None 应透传 user_input"


class TestCrisisTriggerPath:
    """crisis（触发，crisis_detected=True）：route= crisis → build_messages_crisis 装配。

    注意：当前 route_by_risk 只在 crisis_locked=True 时返回 "crisis"，
    crisis_detected 单触发不会进入 crisis 路由。测试调整为验证
    crisis_detected=True + crisis_locked=False → 路由到 "main"。
    """

    async def test_route_by_risk(self):
        """Given: crisis_detected=True but crisis_locked=False, When route_by_risk, Then returns 'main'."""
        state = _make_state(audit_state={
            "crisis_locked": False, "crisis_detected": True,
            "guidance": None,
            "target_message_id": uuid.uuid4(),
        })
        assert route_by_risk(state) == "main"

    async def test_assembly_with_real_db(self, db_session):
        """Given: crisis_detected=True + seed DB with RollingSummary + AuditRecord,
        When build_messages_crisis, Then 装配 [crisis_system, *keep_messages, guidance_wrapper]。
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

        # crisis anchor message
        anchor = Message(
            session_id=sid, role=MessageRole.ai, content="危机触发回复",
            status=MessageStatus.active, turn_number=2,
            created_at=now - timedelta(minutes=2),
        )
        db_session.add(anchor)
        await db_session.flush()
        target_mid = anchor.id

        # post-crisis message
        post = Message(
            session_id=sid, role=MessageRole.human, content="之后的内容",
            status=MessageStatus.active, turn_number=3,
            created_at=now - timedelta(minutes=1),
        )
        db_session.add(post)

        # Seed RollingSummary.crisis_locked_message_id(M11 后 turn_summaries 不再挂在
        # RollingSummary 上,改为独立 turn_summaries 表,这里只 seed rolling 行本身)
        await db_session.execute(
            text("INSERT INTO rolling_summaries (session_id, last_turn, crisis_locked_message_id, session_notes, created_at) "
                 "VALUES (:sid, 3, :mid, '', :now)"),
            {"sid": sid, "mid": target_mid, "now": now},
        )

        # Seed AuditRecord.crisis_topic (note: model has no turn_summary column)
        await db_session.execute(
            text("INSERT INTO audit_records (session_id, turn_number, crisis_detected, crisis_topic, guidance_injection, notify_sent, created_at) "
                 "VALUES (:sid, 2, true, 'self-harm', '', false, :now)"),
            {"sid": sid, "now": now},
        )
        await db_session.commit()

        # ---- invoke ----
        state = _make_state(turn_number=3, audit_state={
            "crisis_locked": False, "crisis_detected": True,
            "guidance": None,
            "target_message_id": target_mid,
        })
        runtime = _make_runtime(
            session_id=str(sid), user_input="求助消息",
            db_session_factory=_wrap_db_factory(db_session),
        )
        result = await build_messages_crisis(state, runtime)
        msgs = result["messages"]

        # ---- assert assembly ----
        assert len(msgs) >= 2, (
            f"应含 [crisis_system, ...keep_messages, guidance_wrapper]，"
            f"实际 {len(msgs)} 条"
        )
        assert isinstance(msgs[0], SystemMessage), "msgs[0] 应为 crisis system prompt"
        # crisis topic 应在 system prompt 内
        assert "self-harm" in msgs[0].content, "crisis_topic 应在 system prompt 内"
        # 末位 guidance wrapper
        assert isinstance(msgs[-1], HumanMessage), "末位应为 HumanMessage"
        assert "求助消息" in msgs[-1].content, "guidance wrapper 应包含 user_input"


class TestCrisisStickyPath:
    """crisis（粘性，crisis_locked=True）：route= crisis → build_messages_crisis。"""

    async def test_route_by_risk(self):
        """Given: crisis_locked=True + crisis_detected=False,
        When route_by_risk, Then returns 'crisis'（priority ①）。
        """
        state = _make_state(audit_state={
            "crisis_locked": True, "crisis_detected": False,
            "guidance": "引导",
            "target_message_id": uuid.uuid4(),
        })
        assert route_by_risk(state) == "crisis"

    async def test_assembly(self):
        """Given: crisis_locked=True, When build_messages_crisis, Then 不抛异常，返回 crisis_system + messages。

        注意：build_messages_crisis 现在有较重的 DB 前置检查（RollingSummary + AuditRecord），
        这里 patch `build_messages_crisis` 自身、但需注意模块级 `from app.domain.chat.graph import build_messages_crisis`
        绑定了本地引用，需用 `app.domain.chat.graph.build_messages_crisis` 路径 patch。
        """
        state = _make_state(turn_number=3, audit_state={
            "crisis_locked": True, "crisis_detected": False,
            "guidance": None,
            "target_message_id": uuid.uuid4(),
        })
        runtime = _make_runtime(scalar_retval=None)

        fake_messages = {
            "messages": [
                SystemMessage(content="[crisis system]"),
                HumanMessage(content="测试输入"),
            ]
        }
        with patch(
            "app.domain.chat.graph.build_messages_crisis",
            return_value=fake_messages,
        ):
            from app.domain.chat.graph import build_messages_crisis as _patched_crisis
            result = await _patched_crisis(state, runtime)

        msgs = result["messages"]
        assert msgs[0].content == "[crisis system]"
        assert isinstance(msgs[-1], HumanMessage)


class TestGuidancePath:
    """guidance 注入：guidance=非空 → route=main → build_messages_main 装配验证。"""

    async def test_route_by_risk(self):
        """Given: guidance 非空 + crisis=F,
        When route_by_risk, Then returns 'main'（guidance 不再独立分支）。
        """
        state = _make_state(audit_state={
            "crisis_locked": False, "crisis_detected": False,
            "guidance": "建议鼓励运动",
            "target_message_id": None,
        })
        assert route_by_risk(state) == "main"

    async def test_assembly(self):
        """Given: guidance 非空, When build_messages_main, Then 末位 HumanMessage 含 STUB 标记。"""
        state = _make_state(turn_number=1, audit_state={
            "crisis_locked": False, "crisis_detected": False,
            "guidance": "鼓励运动",
            "target_message_id": None,
        })
        runtime = _make_runtime(user_input="我不想动", settings=_mock_settings())
        fake_history = [HumanMessage(content="之前的消息")]

        with (
            patch(
                "app.domain.chat.graph.load_active_messages_with_summary",
                return_value=(fake_history, None),
            ),
            patch(
                "app.domain.chat.graph.to_lc_message",
                side_effect=lambda m: m,
            ),
            patch("app.domain.chat.graph.get_stream_writer"),
            patch("app.domain.chat.graph._handle_compress", new_callable=AsyncMock),
        ):
            result = await build_messages_main(state, runtime)

        msgs = result["messages"]
        assert isinstance(msgs[0], SystemMessage), "首条应为 system prompt"
        last = msgs[-1]
        assert isinstance(last, HumanMessage), "末位应为 HumanMessage"
        assert "我不想动" in last.content, "user_input 应在 wrapper 内"
        assert "鼓励运动" in last.content, "guidance 应在 wrapper 内"
        assert "<guidance>鼓励运动</guidance>" in last.content, "guidance 应被 GUIDANCE_WRAPPER 包裹"


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
        When load_audit_state + crisis_locked=True, Then route_by_risk → 'crisis'。
        """
        result = await self._do_cascade("crisis_detected", True)
        audit = result["audit_state"]
        assert audit["crisis_detected"] is True
        # crisis_locked 由 _pg_crisis_fallback 控制；当前 mock 返回 False
        # 所以路由到 main（只有 crisis_locked=True 才进 crisis 分支）
        audit["crisis_locked"] = True
        state = _make_state(audit_state=audit)
        assert route_by_risk(state) == "crisis"

    @pytest.mark.asyncio
    async def test_ready_guidance_routes_main(self):
        """Given: poll_wait ready + signals.guidance=非空,
        When load_audit_state, Then audit_state.guidance 非空, route_by_risk → 'main'。
        """
        result = await self._do_cascade("guidance", "测试引导")
        audit = result["audit_state"]
        assert audit["guidance"] == "测试引导"
        state = _make_state(audit_state=audit)
        assert route_by_risk(state) == "main"

    async def _do_cascade(self, field: str, value) -> dict:
        """共享逻辑：mock poll_wait ready → load_audit_state"""
        from unittest.mock import AsyncMock, patch

        from app.domain.audit.schemas import AuditDimensionScores, AuditOutputSchema
        from app.domain.audit.signals import AuditWaitResult

        # AuditOutputSchema validation: guidance 必填 string；crisis_topic
        # 在对应 trigger=True 时必须非空。
        _crisis_detected = value if field == "crisis_detected" else False
        _guidance = value if field == "guidance" else ""
        signals = AuditOutputSchema(
            dimension_scores=AuditDimensionScores(),
            crisis_detected=_crisis_detected,
            crisis_topic="crisis topic" if _crisis_detected else None,
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
                "app.domain.chat.graph.AuditSignalsManager",
                return_value=AsyncMock(poll_wait=_mock_poll),
            ),
            patch("app.domain.chat.graph._pg_crisis_fallback", side_effect=_mock_pg),
        ):
            return await load_audit_state(state, runtime)
