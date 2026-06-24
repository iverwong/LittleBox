"""ARQ Worker entrypoint + 生命周期钩子 + 配置。

聚合 audit + expert 两个 job function 与 cron_jobs，保持 worker 入口中性。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from arq.connections import RedisSettings

from app.core.config import settings

logger = logging.getLogger("worker")

MAX_TRIES = 3

_parsed_redis = urlparse(settings.redis_url)


async def on_startup(ctx: dict[str, Any]) -> None:
    """Worker 启动：通过 RuntimeResources 构建共享资源。

    Args:
        ctx: ARQ worker ctx dict；写入 resources / settings。
    """
    from app.core.runtime import build_runtime

    rr = await build_runtime(settings)
    ctx["resources"] = rr
    ctx["settings"] = settings
    logger.info("worker.startup")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Worker 关闭：teardown RuntimeResources。

    Args:
        ctx: ARQ worker ctx dict；读取 resources。
    """
    from app.core.runtime import teardown_runtime

    await teardown_runtime(ctx["resources"])
    logger.info("worker.shutdown")


async def on_job_start(ctx: dict[str, Any]) -> None:
    """Job 开始日志。

    Args:
        ctx: ARQ worker ctx dict。
    """
    logger.info(
        "worker.job.start job_id=%s job_try=%s",
        ctx.get("job_id", "?"),
        ctx.get("job_try", 1),
    )


async def on_job_end(ctx: dict[str, Any]) -> None:
    """Job 结束日志。

    Args:
        ctx: ARQ worker ctx dict。
    """
    logger.info("worker.job.end job_id=%s", ctx.get("job_id", "?"))


WORKER_SETTINGS: dict[str, Any] = {
    "functions": [
        "app.worker.run_audit",
        "app.domain.expert.worker.run_daily_reports",
    ],
    "redis_settings": RedisSettings(
        host=_parsed_redis.hostname or "localhost",
        port=_parsed_redis.port or 6379,
        password=_parsed_redis.password,
        database=settings.arq_redis_db,
    ),
    "max_tries": MAX_TRIES,
    "job_timeout": 3600,
    "on_startup": on_startup,
    "on_shutdown": on_shutdown,
    "on_job_start": on_job_start,
    "on_job_end": on_job_end,
    "cron_jobs": [
        {
            "hour": settings.expert_cron_hour,
            "minute": settings.expert_cron_minute,
            "coroutine": "app.domain.expert.worker.run_daily_reports",
        }
    ],
    "ctx": {"settings": settings},
}


# ---------------------------------------------------------------------------
# run_audit — 重新导出（保持与 audit/worker.py 的兼容）
# ---------------------------------------------------------------------------

from app.domain.audit.worker import run_audit  # noqa: E402, F401
