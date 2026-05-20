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

from unittest.mock import AsyncMock, patch

import pytest

from app.chat.graph import load_audit_state
from app.chat.state import MainDialogueState
from app.schemas.audit import AuditDimensionScores, AuditOutputSchema
from app.state.audit_signals import AuditWaitResult


def _make_state(turn_number: int = 2, sid: str = "test-sid") -> MainDialogueState:
    return MainDialogueState(
        session_id=sid,
        child_user_id="child-1",
        child_profile=None,
        provider="deepseek",
        messages=[],
        audit_state={},
        pending_guidance=None,
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
    guidance="test guidance",
    turn_summary="ok",
)


class TestLoadAuditState:
    """7 路径覆盖。"""

    @pytest.mark.asyncio
    async def test_first_turn_returns_all_false(self) -> None:
        """turn_number==1 → 直接返回 all-False，不 poll_wait。"""
        state = _make_state(turn_number=1)
        result = await load_audit_state(state)
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False
        assert audit.get("redline_triggered") is False
        assert audit.get("guidance") is None

    async def _do_poll_wait_test(self, kind, turn_number=2, **kwargs):
        """共享逻辑：patch get_audit_redis + AuditSignalsManager → 调 load_audit_state。"""
        state = _make_state(turn_number=turn_number)

        async def _mock_poll_wait(sid, expected_turn, timeout=None):
            return AuditWaitResult(kind=kind, **kwargs)

        with (
            patch("app.auth.redis_client.get_audit_redis", return_value=AsyncMock()),
            patch(
                "app.chat.graph.AuditSignalsManager",
                return_value=AsyncMock(poll_wait=_mock_poll_wait),
            ),
        ):
            return await load_audit_state(state)

    @pytest.mark.asyncio
    async def test_ready_injects_signals(self) -> None:
        """poll_wait kind=ready → 信号注入。"""
        result = await self._do_poll_wait_test("ready", signals=_AUDIT_OUTPUT)
        audit = result.get("audit_state", {})
        assert audit.get("guidance") == "test guidance"

    @pytest.mark.asyncio
    async def test_failed_returns_all_false(self) -> None:
        """poll_wait kind=failed → all-False。"""
        result = await self._do_poll_wait_test("failed", error="LLM error")
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False

    @pytest.mark.asyncio
    async def test_miss_returns_all_false(self) -> None:
        """poll_wait kind=miss → all-False。"""
        result = await self._do_poll_wait_test("miss")
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False

    @pytest.mark.asyncio
    async def test_turn_mismatch_returns_all_false(self) -> None:
        """poll_wait kind=turn_mismatch → all-False。"""
        result = await self._do_poll_wait_test("turn_mismatch", actual_turn=5)
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False

    @pytest.mark.asyncio
    async def test_timeout_returns_all_false(self) -> None:
        """poll_wait kind=timeout → all-False。"""
        result = await self._do_poll_wait_test("timeout")
        audit = result.get("audit_state", {})
        assert audit.get("crisis_detected") is False

    @pytest.mark.asyncio
    async def test_pending_to_ready(self) -> None:
        """poll_wait kind=ready（含 pending→ready 翻转）→ 信号注入。"""
        result = await self._do_poll_wait_test("ready", signals=_AUDIT_OUTPUT)
        audit = result.get("audit_state", {})
        assert audit.get("guidance") == "test guidance"
