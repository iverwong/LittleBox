"""审查信号管道：Redis `audit:{sid}` 三态读写 + poll_wait 轮询。

`AuditSignalsManager` 封装 Redis SET/GET 操作和 `poll_wait` 轮询协议。
由主对话图 `load_audit_state` / `enqueue_audit` 和 ARQ worker `run_audit` 消费。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from redis.asyncio import Redis
from pydantic import ValidationError

from app.schemas.audit import AuditOutputSchema, AuditSignalsPayload

logger = logging.getLogger("audit.signals")


@dataclass
class AuditWaitResult:
    """`poll_wait` 返回结果，5 种终端状态之一。

    - ready: 审查完成，signals 就绪
    - failed: 审查失败，error 描述原因
    - miss: 管道 key 不存在（可能超期过期或从未写入）
    - turn_mismatch: turn 不匹配，actual_turn 为实际值
    - timeout: deadline 到达仍为 pending
    """

    kind: Literal["ready", "failed", "miss", "turn_mismatch", "timeout"]
    signals: AuditOutputSchema | None = None
    actual_turn: int | None = None
    error: str | None = None


class AuditSignalsManager:
    """Redis `audit:{sid}` 信号管道管理器。

    三态语义（D3 决议）：
    - pending: 已入队，worker 尚未完成
    - ready: 审查完成，signals 可读取
    - failed: 重试用尽，error 描述失败原因

    TTL（config.audit_redis_ttl_seconds，默认 24h）在 SET 时由 `ex` 参数原子写入。
    """

    def __init__(
        self,
        redis: Redis,
        ttl: int,
        poll_interval: float = 0.25,
        poll_timeout: float = 30.0,
    ):
        self._redis = redis
        self._ttl = ttl
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

    async def set_pending(
        self, sid: str, turn: int, started_at: str,
    ) -> None:
        """SET `audit:{sid}` 为 pending 状态。

        由 `enqueue_audit` 调用（Step 9）。started_at 由调用方传入（UTC ISO-8601）。
        """
        payload = AuditSignalsPayload(
            status="pending", turn=turn, started_at=started_at,
        )
        await self._redis.set(
            f"audit:{sid}", payload.model_dump_json(), ex=self._ttl,
        )

    async def set_ready(
        self, sid: str, turn: int, signals: AuditOutputSchema,
        completed_at: str | None = None,
    ) -> None:
        """SET `audit:{sid}` 为 ready 状态。

        由 ARQ worker `run_audit` 调用（Step 7）。
        completed_at 通常应由 worker 传入（UTC ISO-8601）；None 仅用于测试。
        """
        payload = AuditSignalsPayload(
            status="ready", turn=turn, signals=signals,
            completed_at=completed_at,
        )
        await self._redis.set(
            f"audit:{sid}", payload.model_dump_json(), ex=self._ttl,
        )

    async def set_failed(
        self, sid: str, turn: int, error: str,
        completed_at: str | None = None,
    ) -> None:
        """SET `audit:{sid}` 为 failed 状态。

        由 ARQ on_job_failure 钩子调用（Step 7）。
        completed_at 通常应由调用方传入；None 仅用于测试。
        """
        payload = AuditSignalsPayload(
            status="failed", turn=turn, error=error,
            completed_at=completed_at,
        )
        await self._redis.set(
            f"audit:{sid}", payload.model_dump_json(), ex=self._ttl,
        )

    async def get(self, sid: str) -> AuditSignalsPayload | None:
        """GET `audit:{sid}` → AuditSignalsPayload。

        JSON 损坏 / schema 不匹配时 log warning + return None（与 miss 等价降级）。
        """
        raw = await self._redis.get(f"audit:{sid}")
        if raw is None:
            return None
        try:
            return AuditSignalsPayload.model_validate_json(raw)
        except ValidationError:
            logger.warning("audit.signals.invalid sid=%s raw=%s", sid, raw)
            return None

    async def poll_wait(
        self, sid: str, expected_turn: int,
        timeout: float | None = None,
    ) -> AuditWaitResult:
        """轮询 `audit:{sid}` 直到终端状态或超时（D5 决议）。

        6 路径分发（D6 turn 校验）：
        - key 不存在 → miss
        - turn != expected_turn → turn_mismatch（严重落后或数据错乱）
        - status=ready → ready
        - status=failed → failed
        - status=pending → 继续轮询
        - deadline 到达 → timeout

        timeout 默认 self._poll_timeout（构造注入，prod=30s，测试可覆盖）。
        """
        deadline = time.monotonic() + (
            timeout if timeout is not None else self._poll_timeout
        )
        while True:
            raw = await self._redis.get(f"audit:{sid}")
            if raw is None:
                return AuditWaitResult(kind="miss")

            try:
                payload = AuditSignalsPayload.model_validate_json(raw)
            except ValidationError:
                logger.warning(
                    "audit.signals.invalid sid=%s raw=%s", sid, raw,
                )
                return AuditWaitResult(kind="miss")

            if payload.turn != expected_turn:
                # D6：turn 严格不等即 mismatch，不论大小
                return AuditWaitResult(
                    kind="turn_mismatch", actual_turn=payload.turn,
                )

            if payload.status == "ready":
                return AuditWaitResult(
                    kind="ready", signals=payload.signals,
                )

            if payload.status == "failed":
                return AuditWaitResult(
                    kind="failed", error=payload.error,
                )

            # status == "pending" → 继续等
            if time.monotonic() >= deadline:
                return AuditWaitResult(kind="timeout")

            await asyncio.sleep(self._poll_interval)
