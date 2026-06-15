"""ARQ Worker entrypoint + run_audit job + 失败标记（M8 Step 7 / T10 RuntimeResources）。

Worker 配置：
- max_tries=3，超限后 ARQ 自动 dead-letter
- 无 `on_job_failure`（arq 0.28 无此钩子），失败标记在 `run_audit` 内
  判断 `ctx['job_try'] >= max_tries` 时写入
- on_startup / on_shutdown / on_job_start / on_job_end 均为日志桩

D14 协议：
- 前 N-1 次重试失败 → raise 触发 ARQ retry，不写 Redis
- 第 N 次（job_try == max_tries）→ set_failed + raise（ARQ 自动 dead-letter）

T10 变更（D-patch0-7）：
- on_startup 通过 build_runtime(settings) 构造 RuntimeResources
- on_shutdown 通过 teardown_runtime(ctx["resources"]) 清理
- run_audit 从 ctx["resources"] 取 audit_graph + db_session_factory + audit_redis，
  构造 AuditContextSchema 调用 rr.audit_graph.ainvoke(state, context=audit_ctx)
- 签名新增 child_user_id：由 enqueue_audit 从 ChatContextSchema 取值下传，
  避免 worker 内 SELECT 反查（方案 α）
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from arq.connections import RedisSettings

from app.core.config import settings
from app.domain.accounts.schemas import ChildProfileSnapshot
from app.domain.audit.graph import AuditGraphState
from app.domain.audit.signals import AuditSignalsManager

logger = logging.getLogger("audit.worker")

# 预解析 Redis URL 供 WORKER_SETTINGS 使用
_parsed_redis = urlparse(settings.redis_url)


# ---------------------------------------------------------------------------
# Worker 生命周期钩子（T10：RuntimeResources 双对称）
# ---------------------------------------------------------------------------


async def on_startup(ctx: dict[str, Any]) -> None:
    """Worker 启动：通过 RuntimeResources 构建共享资源。"""
    from app.core.runtime import build_runtime

    rr = await build_runtime(settings)
    ctx["resources"] = rr
    ctx["settings"] = settings  # 保留兼容
    ctx["signals_manager"] = AuditSignalsManager(
        rr.audit_redis,  # 单一 Redis 来源（§A.1）
        ttl=settings.audit_redis_ttl_seconds,
    )
    logger.info("audit.worker.startup")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Worker 关闭：teardown RuntimeResources 替换 dispose_engine()。"""
    from app.core.runtime import teardown_runtime

    await teardown_runtime(ctx["resources"])
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
    "functions": ["app.domain.audit.worker.run_audit"],
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


async def run_audit(
    ctx: dict[str, Any],
    sid: str,
    turn_number: int,
    child_user_id: str,
    target_message_id: str,
    child_profile: dict,  # 入队 asdict，出队重建实例
) -> None:
    """执行一次审查（ARQ job function）。

    ARQ 约定：job function 的第一个参数是 ctx dict，之后为自定义参数。
    ctx 包含 RuntimeResources（on_startup 构造）+ settings + signals_manager。

    D14 语义：
    - 成功 → set_ready
    - 失败 + 还有重试机会 → raise（触发 ARQ retry）
    - 失败 + 已到 max_tries → set_failed + raise（ARQ 会 dead-letter）

    T10（D-patch0-7）：child_user_id 由 enqueue_audit 下传，方案 α 避免 worker 内 SELECT。
    """
    import uuid

    from app.core.runtime import RuntimeResources
    from app.domain.audit.context_schema import AuditContextSchema

    rr: RuntimeResources = ctx["resources"]
    manager: AuditSignalsManager = ctx["signals_manager"]
    # 重建，确保新增字段通过默认值传入，而非报错
    snapshot = ChildProfileSnapshot(**child_profile)

    try:
        audit_ctx = AuditContextSchema(
            session_id=uuid.UUID(sid),
            child_user_id=uuid.UUID(child_user_id),
            target_message_id=uuid.UUID(target_message_id),
            max_iter=rr.settings.max_audit_tool_iterations,
            child_profile=snapshot,
            settings=rr.settings,
            db_session_factory=rr.db_session_factory,
            audit_redis=rr.audit_redis,
        )
        state: AuditGraphState = {
            "sid": sid,
            "turn_number": turn_number,
            "session_notes_working": "",
            "tool_iter_count": 0,
            "structured_output": None,
            "messages": [],
        }
        result: dict[str, Any] = await rr.audit_graph.ainvoke(
            state,
            context=audit_ctx,  # type: ignore[reportArgumentType]
            # LangSmith trace 配置：按 session_id / child_id 过滤 trace。
            # 当前调用点原本无 config，无既有键需合并（无 checkpointer /
            # callbacks / configurable 既有键）。
            config={
                "run_name": "audit",
                "metadata": {
                    "session_id": str(audit_ctx.session_id),
                    "child_id": str(audit_ctx.child_user_id),
                    "turn_number": turn_number,
                    "target_message_id": str(audit_ctx.target_message_id),
                },
                "tags": ["audit"],
            },
        )
        output = result.get("structured_output")
        if output is not None:
            await manager.set_ready(
                sid,
                turn_number,
                output,
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
                sid,
                turn_number,
                str(e),
                completed_at=datetime.now(UTC).isoformat(),
            )
        raise
