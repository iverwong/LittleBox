"""load_audit_state 7 路径覆盖（首轮 1 + 6 分支）。

6 分支（非首轮 poll_wait 结果）：
  ready → 信号注入
  failed → all-False + 日志
  miss → all-False + 日志
  turn_mismatch → all-False + 日志
  timeout → all-False + 日志
  pending→ready → 异步翻转后信号注入（与 ready 共享 kind=ready 分支路径）
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.chat.graph import load_audit_state
from app.chat.state import MainDialogueState
from app.domain.audit.schemas import AuditDimensionScores, AuditOutputSchema
from app.domain.audit.signals import AuditWaitResult

pytestmark = [
    pytest.mark.audit,
    pytest.mark.asyncio,
]


def _make_fake_runtime(sid: str = "00000000-0000-0000-0000-000000000001") -> object:
    """构造最小 Runtime[ChatContextSchema] 替代（LangGraph 注入 mock）。

    测试中直接调 load_audit_state(state, runtime)，runtime 仅提供
    .context.session_id / .context.audit_redis / .context.settings 三个属性。
    """
    from types import SimpleNamespace

    from app.chat.context_schema import ChatContextSchema

    ctx = ChatContextSchema(
        session_id=sid,
        child_user_id="child-1",
        child_profile={},
        age=8,
        gender=None,
        user_input="test",
        settings=SimpleNamespace(
            main_provider="deepseek",
            deepseek_api_key="",
            deepseek_base_url="https://api.deepseek.com/v1",
            deepseek_model="deepseek-v4-flash",
            main_thinking_enabled=True,
            main_reasoning_effort="max",
            bailian_api_key="",
            bailian_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            bailian_model="deepseek-v4-flash",
            llm_request_timeout_seconds=60.0,
            enable_fallback=False,
            fallback_provider=None,
            audit_redis_ttl_seconds=86400,
            audit_wait_timeout_seconds=30,
        ),
        db_session_factory=AsyncMock(),
        audit_redis=AsyncMock(),
    )
    return SimpleNamespace(context=ctx)


def _make_state(turn_number: int = 2) -> MainDialogueState:
    return MainDialogueState(
        messages=[],
        audit_state={},
        generated_token_count=0,
        client_alive=True,
        user_stop_requested=False,
        turn_number=turn_number,
    )


_AUDIT_OUTPUT = AuditOutputSchema(
    dimension_scores=AuditDimensionScores(),
    crisis_detected=False,
    crisis_topic=None,
    redline_triggered=False,
    redline_detail=None,
    guidance_injection="test guidance",
    turn_summary="ok",
)


class TestLoadAuditState:
    """7 路径覆盖。"""

    @pytest.mark.asyncio
    async def test_first_turn_returns_all_false(self) -> None:
        """turn_number==1 → 直接返回 all-False，不 poll_wait。"""
        state = _make_state(turn_number=1)
        runtime = _make_fake_runtime()
        result = await load_audit_state(state, runtime)
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False
        assert audit.get("redline_triggered") is False
        assert audit.get("guidance") is None

    async def _do_poll_wait_test(self, kind, turn_number=2, pg_locked=False, **kwargs):
        """共享逻辑：patch AuditSignalsManager + _pg_crisis_fallback → 调 load_audit_state。"""
        state = _make_state(turn_number=turn_number)
        runtime = _make_fake_runtime()

        async def _mock_poll_wait(sid, expected_turn, timeout=None):
            return AuditWaitResult(kind=kind, **kwargs)

        if pg_locked:
            async def _mock_pg(ctx):
                return {"crisis_locked": True, "target_message_id": uuid.UUID(int=1)}
        else:
            async def _mock_pg(ctx):
                return {"crisis_locked": False, "target_message_id": None}

        with (
            patch(
                "app.chat.graph.AuditSignalsManager",
                return_value=AsyncMock(poll_wait=_mock_poll_wait),
            ),
            patch("app.chat.graph._pg_crisis_fallback", side_effect=_mock_pg),
        ):
            return await load_audit_state(state, runtime)

    @pytest.mark.asyncio
    async def test_ready_injects_signals(self) -> None:
        """ready + PG miss → 当轮信号注入, crisis_locked=False。"""
        result = await self._do_poll_wait_test("ready", signals=_AUDIT_OUTPUT)
        audit = result.get("audit_state", {})
        assert audit.get("guidance") == "test guidance"
        assert audit.get("crisis_locked") is False
        assert audit.get("target_message_id") is None

    @pytest.mark.asyncio
    async def test_ready_pg_hit_crisis_locked(self) -> None:
        """ready + PG 非空 → crisis_locked=True + 当轮 signals 独立注入（粘性与当轮检测分离）。"""
        result = await self._do_poll_wait_test(
            "ready", pg_locked=True, signals=_AUDIT_OUTPUT,
        )
        audit = result.get("audit_state", {})
        assert audit.get("crisis_locked") is True  # PG 粘性
        assert audit.get("crisis_detected") is False  # 当轮 signals（_AUDIT_OUTPUT 默认 False）
        assert audit.get("target_message_id") == uuid.UUID(int=1)
        assert audit.get("guidance") == "test guidance"

    @pytest.mark.asyncio
    async def test_failed_returns_all_false(self) -> None:
        """failed + PG miss → all-False + 日志。"""
        result = await self._do_poll_wait_test("failed", error="LLM error")
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False
        assert audit.get("crisis_locked") is False
        assert audit.get("target_message_id") is None

    @pytest.mark.asyncio
    async def test_miss_returns_all_false(self) -> None:
        """miss + PG miss → all-False + 日志。"""
        result = await self._do_poll_wait_test("miss")
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False
        assert audit.get("crisis_locked") is False

    @pytest.mark.asyncio
    async def test_turn_mismatch_returns_all_false(self) -> None:
        """turn_mismatch + PG miss → all-False + 日志。"""
        result = await self._do_poll_wait_test("turn_mismatch", actual_turn=5)
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False
        assert audit.get("crisis_locked") is False

    @pytest.mark.asyncio
    async def test_timeout_returns_all_false(self) -> None:
        """timeout + PG miss → all-False + 日志。"""
        result = await self._do_poll_wait_test("timeout")
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False
        assert audit.get("crisis_locked") is False

    @pytest.mark.asyncio
    async def test_pending_to_ready(self) -> None:
        """pending→ready + PG miss → 信号注入 + crisis_locked=False。"""
        result = await self._do_poll_wait_test("ready", signals=_AUDIT_OUTPUT)
        audit = result.get("audit_state", {})
        assert audit.get("guidance") == "test guidance"
        assert audit.get("crisis_locked") is False

    @pytest.mark.asyncio
    async def test_degradation_with_pg_hit(self) -> None:
        """failed + PG locked → crisis_locked=True + target_message_id=UUID + 当轮 all-False。"""
        result = await self._do_poll_wait_test("failed", pg_locked=True, error="LLM error")
        audit = result.get("audit_state", {})
        assert audit.get("crisis_locked") is True
        assert audit.get("crisis_detected") is False  # 当轮 all-False
        assert audit.get("target_message_id") == uuid.UUID(int=1)
        assert audit.get("guidance") is None

    @pytest.mark.asyncio
    async def test_first_turn_returns_all_false(self) -> None:
        """turn_number==1 → 直接返回 all-False，不 poll_wait 也不查 PG。"""
        state = _make_state(turn_number=1)
        runtime = _make_fake_runtime()
        mock_pg = AsyncMock()
        with patch("app.chat.graph._pg_crisis_fallback", mock_pg):
            result = await load_audit_state(state, runtime)
        mock_pg.assert_not_called()  # 首轮不走 PG
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False
        assert audit.get("redline_triggered") is False
        assert audit.get("guidance") is None
