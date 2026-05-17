"""Audit 信号管道测试：三态读写 + poll_wait 6 路径 + TTL + 覆盖语义。"""
from __future__ import annotations

import asyncio

import pytest
from fakeredis.aioredis import FakeRedis

from app.schemas.audit import AuditDimensionScores, AuditOutputSchema
from app.state.audit_signals import AuditSignalsManager

ISO_NOW = "2026-05-17T10:00:00+00:00"
_AUDIT_OUT = AuditOutputSchema(
    dimension_scores=AuditDimensionScores(),
    crisis_detected=False,
    crisis_topic=None,
    redline_triggered=False,
    redline_detail=None,
    guidance="ok",
    turn_summary="ok",
)


@pytest.fixture
def redis():
    r = FakeRedis(decode_responses=True)
    yield r


@pytest.fixture
def manager(redis):
    return AuditSignalsManager(
        redis, ttl=86400, poll_interval=0.01, poll_timeout=0.05,
    )


@pytest.mark.asyncio
class TestSetAndGet:
    """三态基础读写。"""

    async def test_set_pending(self, manager, redis):
        await manager.set_pending("s1", turn=1, started_at=ISO_NOW)
        raw = await redis.get("audit:s1")
        assert raw is not None
        assert '"status":"pending"' in raw

    async def test_set_ready(self, manager, redis):
        await manager.set_ready("s1", turn=1, signals=_AUDIT_OUT, completed_at=ISO_NOW)
        raw = await redis.get("audit:s1")
        assert raw is not None
        assert '"status":"ready"' in raw

    async def test_set_failed(self, manager, redis):
        await manager.set_failed("s1", turn=1, error="timeout", completed_at=ISO_NOW)
        raw = await redis.get("audit:s1")
        assert raw is not None
        assert '"status":"failed"' in raw

    async def test_set_pending_ttl(self, manager, redis):
        """写入后 TTL ∈ (86000, 86400]，而非 -1 / -2。"""
        await manager.set_pending("s1", turn=1, started_at=ISO_NOW)
        ttl = await redis.ttl("audit:s1")
        assert 86000 < ttl <= 86400, f"TTL out of range: {ttl}"

    async def test_set_overwrite(self, manager, redis):
        """SET 覆盖语义：二次写入覆盖前值，非 SETNX。"""
        await manager.set_pending("s1", turn=1, started_at=ISO_NOW)
        await manager.set_pending("s1", turn=2, started_at=ISO_NOW)
        payload = await manager.get("s1")
        assert payload is not None
        assert payload.turn == 2
        assert payload.status == "pending"

    async def test_get_returns_ready_signals(self, manager):
        await manager.set_ready("s1", turn=1, signals=_AUDIT_OUT, completed_at=ISO_NOW)
        payload = await manager.get("s1")
        assert payload is not None
        assert payload.status == "ready"
        assert payload.signals is not None
        assert payload.signals.guidance == "ok"

    async def test_get_returns_none_for_missing(self, manager):
        payload = await manager.get("nonexistent")
        assert payload is None

    async def test_get_corrupted_json_returns_none(self, manager, redis):
        """损坏 JSON → log warning + return None。"""
        await redis.set("audit:s1", "not-json{{}}")
        payload = await manager.get("s1")
        assert payload is None


@pytest.mark.asyncio
class TestPollWait:
    """poll_wait 6 路径分发。"""

    async def test_miss(self, manager):
        result = await manager.poll_wait("s1", expected_turn=1)
        assert result.kind == "miss"

    async def test_ready_direct(self, manager):
        await manager.set_ready("s1", turn=1, signals=_AUDIT_OUT)
        result = await manager.poll_wait("s1", expected_turn=1)
        assert result.kind == "ready"
        assert result.signals is not None

    async def test_pending_to_ready(self, manager):
        """asyncio.create_task 异步翻转 → poll_wait 返回 ready。"""
        await manager.set_pending("s1", turn=1, started_at=ISO_NOW)

        async def flip():
            await asyncio.sleep(0.01)
            await manager.set_ready("s1", turn=1, signals=_AUDIT_OUT)

        task = asyncio.create_task(flip())
        result = await manager.poll_wait("s1", expected_turn=1)
        await task

        assert result.kind == "ready"

    async def test_failed_direct(self, manager):
        await manager.set_failed("s1", turn=1, error="LLM error")
        result = await manager.poll_wait("s1", expected_turn=1)
        assert result.kind == "failed"
        assert result.error == "LLM error"

    async def test_turn_mismatch(self, manager):
        """D6：turn 严格不等即 mismatch（不论大小）。"""
        await manager.set_pending("s1", turn=2, started_at=ISO_NOW)
        result = await manager.poll_wait("s1", expected_turn=1)
        assert result.kind == "turn_mismatch"
        assert result.actual_turn == 2

    async def test_timeout(self, manager):
        """pending 始终不翻转 → deadline → timeout。"""
        await manager.set_pending("s1", turn=1, started_at=ISO_NOW)
        result = await manager.poll_wait("s1", expected_turn=1)
        assert result.kind == "timeout"


class TestBuildArqRedisUrl:
    """_build_arq_redis_url 纯函数测试。"""

    def test_arq_url_ends_with_db_1(self):
        from app.auth.redis_client import _build_arq_redis_url
        url = _build_arq_redis_url()
        assert url.endswith("/1"), f"Expected /1 suffix, got {url}"
