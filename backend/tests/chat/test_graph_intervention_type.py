"""Graph-node 级 intervention_type 发射测试：真实图 + 真实 route_by_risk，仅 mock LLM astream。

D-patch1-2 补遗 · 6 条收口必办（闸门 A 裁决）全部内置：
  A. patch 靶点：app.domain.chat.graph.build_main_llm / _crisis_llm / _redline_llm
  B. audit_state 完整 5 键 AuditState
  C. ChatContextSchema 含真实 settings + db_session_factory + 种子数据
  D. load_audit_state patch 早于 build_main_graph()
  E. FakeLLM 产非空 content + usage_metadata=None + 无 finish_reason/reasoning 污染
  F. initial_state 含 turn_number + messages
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from app.domain.chat.graph import build_main_graph
from app.domain.chat.state import AuditState, MainDialogueState
from tests.conftest import make_chat_context, make_child_profile_snapshot
from app.core.config import settings
from app.core.enums import UserRole
from app.domain.accounts.models import Family, FamilyMember, User
from app.domain.chat.models import Message, Session
from langchain_core.messages import AIMessageChunk, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio(loop_scope="function")

TABLES = ["families", "users", "family_members", "sessions", "messages"]

# 单 chunk 供所有测试共用：content 非空、usagemeta None、无 finish_reason 污染
_CHUNK = AIMessageChunk(content="hello world", id="fake-1", usage_metadata=None)


class FakeLLM:
    """可控 AIMessageChunk 流，替代真实 LLM astream I/O。"""

    def __init__(self, chunks: list[AIMessageChunk] | None = None):
        self._chunks = chunks or [_CHUNK]

    async def astream(self, messages):
        for c in self._chunks:
            yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    crisis_locked: bool = False,
    crisis_detected: bool = False,
    redline_triggered: bool = False,
    guidance: str | None = None,
    target_message_id: str | None = None,
) -> AuditState:
    """构造完整 5 键 AuditState，与 _all_false_audit_state 同形。"""
    return {
        "crisis_locked": crisis_locked,
        "crisis_detected": crisis_detected,
        "redline_triggered": redline_triggered,
        "guidance": guidance,
        "target_message_id": target_message_id,
    }


def _stub_load_audit(audit: AuditState):
    """返回替换 load_audit_state 的 stub node。"""
    async def _fn(state, runtime):
        return {"audit_state": audit}
    return _fn


async def _seed_family_child(seed_sess: AsyncSession) -> User:
    """种子：family → child user → family_member。返回 User。"""
    fam = Family()
    seed_sess.add(fam)
    await seed_sess.flush()

    child = User(
        family_id=fam.id, role=UserRole.child, phone="test-graph-it", is_active=True,
    )
    seed_sess.add(child)
    await seed_sess.flush()

    seed_sess.add(FamilyMember(family_id=fam.id, user_id=child.id, role=UserRole.child))
    await seed_sess.flush()
    return child


async def _seed_session(seed_sess: AsyncSession, child: User) -> tuple[uuid4, uuid4]:
    """种子：session 行。返回 (sid, child_user_id)。"""
    sid = uuid4()
    seed_sess.add(Session(id=sid, child_user_id=child.id, title="test"))
    await seed_sess.flush()
    return sid, child.id


async def _seed_crisis_anchor(seed_sess: AsyncSession, sid: uuid4) -> uuid4:
    """种子：一条 message 作为 crisis anchor。返回其 id。"""
    msg = Message(
        session_id=sid, role="human", content="crisis anchor", status="active",
    )
    seed_sess.add(msg)
    await seed_sess.flush()
    return msg.id


# ---------------------------------------------------------------------------
# Crisis 路由：crisis_locked → intervention_type="crisis" 先于 delta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crisis_emits_crisis(concurrent_db_sessions, engine):
    """crisis_locked → route_by_risk 走 crisis → call_crisis_llm 发射 'crisis' 且先于首 delta。"""
    sessions = await concurrent_db_sessions(count=1, tables=TABLES)
    seed_sess = sessions[0]

    child = await _seed_family_child(seed_sess)
    sid, child_uid = await _seed_session(seed_sess, child)
    anchor_id = await _seed_crisis_anchor(seed_sess, sid)
    await seed_sess.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    audit = _audit(crisis_locked=True, target_message_id=anchor_id)
    ctx = make_chat_context(
        session_id=sid, child_user_id=child_uid, user_input="test",
        settings=settings, db_session_factory=factory,
        audit_redis=AsyncMock(),
        profile=make_child_profile_snapshot(age=8, gender="male"),
    )

    with (
        patch("app.domain.chat.graph.load_audit_state", _stub_load_audit(audit)),
        patch("app.domain.chat.graph.build_crisis_llm", return_value=FakeLLM()),
    ):
        graph = build_main_graph()
        state: MainDialogueState = {
            "messages": [],
            "audit_state": audit,
            "generated_token_count": 0,
            "client_alive": True,
            "user_stop_requested": False,
            "turn_number": 1,
        }
        captured: list[dict] = []
        async for payload in graph.astream(state, context=ctx, stream_mode="custom"):
            captured.append(payload)

    it_payloads = [p for p in captured if "intervention_type" in p]
    assert len(it_payloads) >= 1, "crisis 路由应有 intervention_type payload"
    assert it_payloads[0]["intervention_type"] == "crisis"

    delta_payloads = [p for p in captured if "delta" in p]
    assert len(delta_payloads) >= 1, "crisis 路由应有 delta payload"

    it_idx = next(i for i, p in enumerate(captured) if "intervention_type" in p)
    delta_idx = next(i for i, p in enumerate(captured) if "delta" in p)
    assert it_idx < delta_idx, "intervention_type 必须在首个 delta 之前"


# ---------------------------------------------------------------------------
# Redline 路由：redline_triggered → intervention_type="redline" 先于 delta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redline_emits_redline(concurrent_db_sessions, engine):
    """redline → call_redline_llm 发射 'redline' 且先于 delta。"""
    sessions = await concurrent_db_sessions(count=1, tables=TABLES)
    seed_sess = sessions[0]

    child = await _seed_family_child(seed_sess)
    sid, child_uid = await _seed_session(seed_sess, child)
    await seed_sess.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    audit = _audit(redline_triggered=True)
    ctx = make_chat_context(
        session_id=sid, child_user_id=child_uid, user_input="test",
        settings=settings, db_session_factory=factory,
        audit_redis=AsyncMock(),
        profile=make_child_profile_snapshot(age=8, gender="male"),
    )

    with (
        patch("app.domain.chat.graph.load_audit_state", _stub_load_audit(audit)),
        patch("app.domain.chat.graph.build_redline_llm", return_value=FakeLLM()),
    ):
        graph = build_main_graph()
        state: MainDialogueState = {
            "messages": [],
            "audit_state": audit,
            "generated_token_count": 0,
            "client_alive": True,
            "user_stop_requested": False,
            "turn_number": 1,
        }
        captured: list[dict] = []
        async for payload in graph.astream(state, context=ctx, stream_mode="custom"):
            captured.append(payload)

    it_payloads = [p for p in captured if "intervention_type" in p]
    assert len(it_payloads) >= 1, "redline 路由应有 intervention_type payload"
    assert it_payloads[0]["intervention_type"] == "redline"

    delta_payloads = [p for p in captured if "delta" in p]
    assert len(delta_payloads) >= 1, "redline 路由应有 delta payload"

    it_idx = next(i for i, p in enumerate(captured) if "intervention_type" in p)
    delta_idx = next(i for i, p in enumerate(captured) if "delta" in p)
    assert it_idx < delta_idx, "intervention_type 必须在首个 delta 之前"


# ---------------------------------------------------------------------------
# Guided 路由：guidance="…" → route_by_risk 走 guidance → call_main_llm → "guided"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guided_emits_guided(concurrent_db_sessions, engine):
    """guidance 非 None → route_by_risk 走 guidance → call_main_llm 发射 'guided' 且先于首 delta。

    与 test_normal_emits_nothing 仅 guidance 字段不同，证明同源契约。
    """
    sessions = await concurrent_db_sessions(count=1, tables=TABLES)
    seed_sess = sessions[0]

    child = await _seed_family_child(seed_sess)
    sid, child_uid = await _seed_session(seed_sess, child)
    await seed_sess.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    audit = _audit(guidance="be gentle")  # 唯一差异：guidance 非 None
    ctx = make_chat_context(
        session_id=sid, child_user_id=child_uid, user_input="test",
        settings=settings, db_session_factory=factory,
        audit_redis=AsyncMock(),
        profile=make_child_profile_snapshot(age=8, gender="male"),
    )

    with (
        patch("app.domain.chat.graph.load_audit_state", _stub_load_audit(audit)),
        patch("app.domain.chat.graph.build_main_llm", return_value=FakeLLM()),
    ):
        graph = build_main_graph()
        # pre-set messages → build_messages_main 早退，不碰 DB
        state: MainDialogueState = {
            "messages": [HumanMessage(content="pre-set")],
            "audit_state": audit,
            "generated_token_count": 0,
            "client_alive": True,
            "user_stop_requested": False,
            "turn_number": 1,
        }
        captured: list[dict] = []
        async for payload in graph.astream(state, context=ctx, stream_mode="custom"):
            captured.append(payload)

    it_payloads = [p for p in captured if "intervention_type" in p]
    assert len(it_payloads) >= 1, "guided 路由应有 intervention_type payload"
    assert it_payloads[0]["intervention_type"] == "guided"

    delta_payloads = [p for p in captured if "delta" in p]
    assert len(delta_payloads) >= 1, "guided 路由应有 delta payload"

    it_idx = next(i for i, p in enumerate(captured) if "intervention_type" in p)
    delta_idx = next(i for i, p in enumerate(captured) if "delta" in p)
    assert it_idx < delta_idx, "intervention_type 必须在首个 delta 之前"


# ---------------------------------------------------------------------------
# Normal 路由：guidance=None → route_by_risk 走 main → call_main_llm 不发射
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_emits_nothing(concurrent_db_sessions, engine):
    """guidance=None → route_by_risk 走 main → captured 中不含任何 intervention_type payload。

    与 test_guided_emits_guided 仅 guidance 字段不同，同源回归守护。
    任一方将来分歧（路由改判据而 call_main_llm 未同步）此例必红。
    """
    sessions = await concurrent_db_sessions(count=1, tables=TABLES)
    seed_sess = sessions[0]

    child = await _seed_family_child(seed_sess)
    sid, child_uid = await _seed_session(seed_sess, child)
    await seed_sess.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    audit = _audit(guidance=None)  # 唯一差异：guidance None
    ctx = make_chat_context(
        session_id=sid, child_user_id=child_uid, user_input="test",
        settings=settings, db_session_factory=factory,
        audit_redis=AsyncMock(),
        profile=make_child_profile_snapshot(age=8, gender="male"),
    )

    with (
        patch("app.domain.chat.graph.load_audit_state", _stub_load_audit(audit)),
        patch("app.domain.chat.graph.build_main_llm", return_value=FakeLLM()),
    ):
        graph = build_main_graph()
        state: MainDialogueState = {
            "messages": [HumanMessage(content="pre-set")],
            "audit_state": audit,
            "generated_token_count": 0,
            "client_alive": True,
            "user_stop_requested": False,
            "turn_number": 1,
        }
        captured: list[dict] = []
        async for payload in graph.astream(state, context=ctx, stream_mode="custom"):
            captured.append(payload)

    # 断言：不含任何 intervention_type payload
    it_payloads = [p for p in captured if "intervention_type" in p]
    assert len(it_payloads) == 0, (
        f"normal 路由不应有 intervention_type payload，捕获 {len(it_payloads)} 个"
    )

    # delta 仍应有
    delta_payloads = [p for p in captured if "delta" in p]
    assert len(delta_payloads) >= 1, "normal 路由应有 delta payload"
