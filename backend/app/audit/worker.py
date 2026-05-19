"""ARQ Worker entrypoint + run_audit job + 失败标记（M8 Step 7）。

Worker 配置：
- max_tries=3，超限后 ARQ 自动 dead-letter
- 无 `on_job_failure`（arq 0.28 无此钩子），失败标记在 `run_audit` 内
  判断 `ctx['job_try'] >= max_tries` 时写入
- on_startup / on_shutdown / on_job_start / on_job_end 均为日志桩

D14 协议：
- 前 N-1 次重试失败 → raise 触发 ARQ retry，不写 Redis
- 第 N 次（job_try == max_tries）→ set_failed + raise（ARQ 自动 dead-letter）
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from arq.connections import RedisSettings

from app.audit.graph import AuditGraphState, build_audit_graph
from app.config import settings
from app.db import dispose_engine
from app.state.audit_signals import AuditSignalsManager

logger = logging.getLogger("audit.worker")

# 预解析 Redis URL 供 WORKER_SETTINGS 使用
_parsed_redis = urlparse(settings.redis_url)


# ---------------------------------------------------------------------------
# Worker 生命周期钩子
# ---------------------------------------------------------------------------


async def on_startup(ctx: dict[str, Any]) -> None:
    """Worker 启动：设置 ctx 中的共享资源。"""
    ctx["settings"] = settings
    ctx["signals_manager"] = AuditSignalsManager(
        ctx["redis"],
        ttl=settings.audit_redis_ttl_seconds,
    )
    logger.info("audit.worker.startup")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Worker 关闭：释放 DB 连接池。"""
    await dispose_engine()
    logger.info("audit.worker.shutdown")


async def on_job_start(ctx: dict[str, Any]) -> None:
    """Job 开始日志。"""
    logger.info(
        "audit.turn.start sid=%s turn=%s job_try=%s",
        ctx.get("sid", "?"),
        ctx.get("turn", "?"),
        ctx.get("job_try", 1),
    )


async def on_job_end(ctx: dict[str, Any]) -> None:
    """Job 结束日志。"""
    logger.info("audit.turn.end sid=%s turn=%s", ctx.get("sid", "?"), ctx.get("turn", "?"))


# ---------------------------------------------------------------------------
# Worker 配置
# ---------------------------------------------------------------------------

MAX_TRIES = 3

WORKER_SETTINGS: dict[str, Any] = {
    "functions": ["app.audit.worker.run_audit"],
    "redis_settings": RedisSettings(
        host=_parsed_redis.hostname or "localhost",
        port=_parsed_redis.port or 6379,
        password=_parsed_redis.password,
        database=settings.arq_redis_db,
    ),
    "max_tries": MAX_TRIES,
    "job_timeout": 60,
    "on_startup": on_startup,
    "on_shutdown": on_shutdown,
    "on_job_start": on_job_start,
    "on_job_end": on_job_end,
    "ctx": {"settings": settings},
}


# ---------------------------------------------------------------------------
# run_audit job
# ---------------------------------------------------------------------------


async def run_audit(ctx: dict[str, Any], sid: str, turn_number: int) -> None:
    """执行一次审查（ARQ job function）。

    ARQ 约定：job function 的第一个参数是 ctx dict，之后为自定义参数。
    ctx 包含 'redis'（ArqRedis）+ 自定义 settings。

    D14 语义：
    - 成功 → set_ready
    - 失败 + 还有重试机会 → raise（触发 ARQ retry）
    - 失败 + 已到 max_tries → set_failed + raise（ARQ 会 dead-letter）
    """
    manager: AuditSignalsManager = ctx.get("signals_manager") or AuditSignalsManager(
        ctx["redis"], ttl=settings.audit_redis_ttl_seconds,
    )
    try:
        graph = build_audit_graph(
            max_iter=settings.max_audit_tool_iterations,
            settings=settings,
        )
        state: AuditGraphState = {
            "sid": sid,
            "turn_number": turn_number,
            "child_profile": None,
            "session_notes_working": "",
            "tool_iter_count": 0,
            "structured_output": None,
            "messages": [],
        }
        result: dict[str, Any] = await graph.ainvoke(state)
        output = result.get("structured_output")
        if output is not None:
            await manager.set_ready(
                sid, turn_number, output,
                completed_at=datetime.now(UTC).isoformat(),
            )
        else:
            # 结构化输出为空 → 异常 raise 触发 retry
            raise RuntimeError(f"audit output is None sid={sid} turn={turn_number}")
    except Exception as e:
        job_try: int = ctx.get("job_try", 1)
        if job_try >= MAX_TRIES:
            # D14: 最后尝试失败，写 failed 状态
            await manager.set_failed(
                sid, turn_number, str(e),
                completed_at=datetime.now(UTC).isoformat(),
            )
        raise
