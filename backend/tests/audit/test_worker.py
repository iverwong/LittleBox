"""审查 Worker 测试：run_audit 全路径 + 失败标记语义（D14）。

覆盖 4 场景：
1. happy path → set_ready + 不抛异常
2. 最后尝试失败（job_try=3）→ set_failed + 抛异常
3. 前几次失败（job_try=1）→ set_failed 不调 + 抛异常
4. WorkerSettings 结构正确性
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.audit.worker import MAX_TRIES, run_audit
from app.schemas.audit import AuditDimensionScores, AuditOutputSchema


_AUDIT_OUTPUT = AuditOutputSchema(
    dimension_scores=AuditDimensionScores(),
    crisis_detected=False,
    crisis_topic=None,
    redline_triggered=False,
    redline_detail=None,
    guidance="ok",
    turn_summary="ok",
)


async def _fake_graph_ainvoke_ok(state: dict) -> dict:
    """模拟 graph 返回含有 structured_output 的结果。"""
    return {"structured_output": _AUDIT_OUTPUT}


async def _fake_graph_ainvoke_raise(state: dict) -> dict:
    """模拟 graph 抛异常。"""
    msg = f"LLM error sid={state.get('sid', '?')} turn={state.get('turn_number', '?')}"
    raise RuntimeError(msg)


def _make_ctx(
    *,
    job_try: int = 1,
    sid: str = "test-sid",
    turn: int = 1,
) -> dict:
    """构造模拟 ARQ ctx。"""
    from app.state.audit_signals import AuditSignalsManager
    from fakeredis.aioredis import FakeRedis

    redis = FakeRedis(decode_responses=True)
    return {
        "job_id": f"audit:{sid}:{turn}",
        "job_try": job_try,
        "redis": redis,
        "signals_manager": AuditSignalsManager(redis, ttl=86400),
    }


class TestRunAudit:
    """run_audit job 4 场景。"""

    @pytest.mark.asyncio
    async def test_happy_path_sets_ready(self) -> None:
        """graph 成功 → set_ready 被调 + 状态为 ready。"""
        ctx = _make_ctx()
        mgr = ctx["signals_manager"]

        with patch(
            "app.audit.worker.build_audit_graph",
            return_value=AsyncMock(ainvoke=_fake_graph_ainvoke_ok),
        ):
            await run_audit(ctx, "test-sid", turn_number=1)

        # 验证 Redis 状态
        payload = await mgr.get("test-sid")
        assert payload is not None
        assert payload.status == "ready"

    @pytest.mark.asyncio
    async def test_final_try_sets_failed(self) -> None:
        """job_try=3（max_tries）时 graph 抛异常 → set_failed + 异常上抛。"""
        ctx = _make_ctx(job_try=MAX_TRIES)
        mgr = ctx["signals_manager"]

        with patch(
            "app.audit.worker.build_audit_graph",
            return_value=AsyncMock(ainvoke=_fake_graph_ainvoke_raise),
        ):
            with pytest.raises(RuntimeError, match="LLM error"):
                await run_audit(ctx, "test-sid", turn_number=2)

        # 验证 Redis 状态
        payload = await mgr.get("test-sid")
        assert payload is not None
        assert payload.status == "failed"
        assert payload.error is not None

    @pytest.mark.asyncio
    async def test_early_try_does_not_set_failed(self) -> None:
        """job_try=1 时 graph 抛异常 → set_failed 不调 + 异常上抛。"""
        ctx = _make_ctx(job_try=1)
        mgr = ctx["signals_manager"]

        with patch(
            "app.audit.worker.build_audit_graph",
            return_value=AsyncMock(ainvoke=_fake_graph_ainvoke_raise),
        ):
            with pytest.raises(RuntimeError, match="LLM error"):
                await run_audit(ctx, "test-sid", turn_number=2)

        # 验证 Redis 状态未写入（key 不存在）
        payload = await mgr.get("test-sid")
        assert payload is None


class TestWorkerSettings:
    """WorkerSettings 结构正确性。"""

    def test_worker_settings_has_required_keys(self) -> None:
        from app.audit.worker import WORKER_SETTINGS

        assert "functions" in WORKER_SETTINGS
        assert "redis_settings" in WORKER_SETTINGS
        assert WORKER_SETTINGS["max_tries"] == MAX_TRIES
        assert WORKER_SETTINGS["job_timeout"] == 60
        assert WORKER_SETTINGS["on_startup"] is not None
        assert WORKER_SETTINGS["on_shutdown"] is not None
        assert WORKER_SETTINGS["on_job_start"] is not None
        assert WORKER_SETTINGS["on_job_end"] is not None
