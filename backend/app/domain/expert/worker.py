"""ARQ cron job: run_daily_reports。

在 04:05（Asia/Shanghai）由 ARQ cron 触发，遍历所有活跃孩子生成日终报告。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import SHANGHAI, logical_day, now_utc
from app.domain.accounts.models import ChildProfile
from app.domain.expert.graph import ExpertGraphState

logger = logging.getLogger("expert.worker")

# 六维度 key 列表，对齐 AuditDimensionScores / SensitivityConfig
DIMENSIONS = ["emotional", "social", "values", "boundaries", "academic", "lifestyle"]
# 高维分数阈值（>= 此值计为 high_turn）
_HIGH_SCORE_THRESHOLD = 7


async def _check_crisis_today(
    db: AsyncSession,
    session_id: uuid.UUID,
) -> bool:
    """查询指定 session 内是否有任一 crisis 标记。

    调用方在 worker 层已强制 1:1 invariant(每个逻辑日唯一一条 today_session),
    因此 session 范围即"当日"范围,无需再叠加 child_user_id / created_at 窗口过滤。

    Args:
        db: DB session。
        session_id: 被查询的 session ID(由 caller 传入 today_session_id)。

    Returns:
        True 表示该 session 有 crisis 标记。
    """
    from app.domain.audit.models import AuditRecord

    stmt = select(
        select(AuditRecord.id)
        .where(
            AuditRecord.session_id == session_id,
            AuditRecord.crisis_detected,
        )
        .exists()
    )
    result = await db.scalar(stmt)
    return bool(result)


async def _aggregate_dimensions(
    db: AsyncSession,
    session_id: uuid.UUID,
) -> dict:
    """从指定 session 的 audit_records 聚合维度的 peak / mean / high_ratio。

    仅查询 dimension_scores IS NOT NULL 的记录。
    对每个维度计算：
      - peak：该维度最大分数
      - mean：该维度平均分数
      - high_ratio：分数 >= 7 的记录数 / 总记录数

    调用方在 worker 层已强制 1:1 invariant(每个逻辑日唯一一条 today_session),
    因此 session 范围即"当日"范围,无需再叠加 owned_session_ids / created_at 窗口过滤。

    Args:
        db: DB session。
        session_id: 被查询的 session ID(由 caller 传入 today_session_id)。

    Returns:
        {dim: {"peak": int, "mean": float, "high_ratio": float}} 格式的 dict。
        无数据时各值为 0。
    """
    from app.domain.audit.models import AuditRecord

    stmt = select(AuditRecord.dimension_scores).where(
        AuditRecord.session_id == session_id,
        AuditRecord.dimension_scores.isnot(None),
    )
    rows = (await db.execute(stmt)).scalars().all()

    dim_scores: dict[str, list[int]] = {d: [] for d in DIMENSIONS}
    for ds in rows:
        ds = ds or {}
        for d in DIMENSIONS:
            score = ds.get(d)
            if isinstance(score, (int, float)):
                dim_scores[d].append(int(score))

    summary: dict[str, dict[str, int | float]] = {}
    for d in DIMENSIONS:
        vals = dim_scores[d]
        if vals:
            summary[d] = {
                "peak": max(vals),
                "mean": round(sum(vals) / len(vals), 2),
                "high_ratio": round(
                    sum(1 for v in vals if v >= _HIGH_SCORE_THRESHOLD) / len(vals),
                    4,
                ),
            }
        else:
            summary[d] = {"peak": 0, "mean": 0.0, "high_ratio": 0.0}

    return summary


async def run_daily_reports(ctx: dict[str, Any]) -> None:
    """ARQ cron job：遍历所有活跃孩子生成日终报告。

    并发策略：asyncio.gather + Semaphore(settings.expert_max_concurrent_children)，
    per-child 失败通过 return_exceptions=True 隔离，不波及同批次其余孩子。

    Args:
        ctx: ARQ worker ctx dict（含 resources / settings）。
    """
    from app.core.runtime import RuntimeResources
    from app.domain.accounts.schemas import ChildProfileSnapshot
    from app.domain.expert.context_schema import ExpertContextSchema

    rr: RuntimeResources = ctx["resources"]
    settings = rr.settings

    report_date = logical_day(now_utc(), boundary_hour=4) - timedelta(days=1)
    logger.info("expert.run_daily_reports start report_date=%s", report_date)

    # 逻辑日窗口：report_date 4:00 Shanghai -> report_date+1 4:00 Shanghai
    day_start = datetime.combine(report_date, datetime.min.time()).replace(
        tzinfo=SHANGHAI,
    ) + timedelta(hours=4)
    day_end = day_start + timedelta(days=1)

    # 查所有活跃孩子（JOIN ChildProfile 确保存在画像）
    async with rr.db_session_factory() as db:
        from app.core.enums import UserRole
        from app.domain.accounts.models import ChildProfile, User

        child_stmt = (
            select(User)
            .join(ChildProfile, ChildProfile.child_user_id == User.id)
            .where(User.role == UserRole.child, User.is_active)
        )
        child_rows = (await db.execute(child_stmt)).scalars().all()
        child_ids = [r.id for r in child_rows]

    if not child_ids:
        logger.info("expert.run_daily_reports no_active_children")
        return

    logger.info(
        "expert.run_daily_reports children_count=%d",
        len(child_ids),
    )

    sem = asyncio.Semaphore(settings.expert_max_concurrent_children)

    async def _report_for_child(child_user_id_val: uuid.UUID) -> None:
        """为一个孩子生成日终报告（内部闭包，被 asyncio.gather 并发调用）。"""
        async with sem:
            async with rr.db_session_factory() as child_db:
                # a. owned_session_ids + 当日 session_id
                # 跨域 inline import chat.Session,Worker → Chat 边界允许。
                # 三路处理:
                #   0 session → 当日无 chat,跳过该 child(产品逻辑"有聊才有报")
                #   1 session → 正常路径,取 session.id
                #   ≥2 session → fail loud,被 return_exceptions=True 兜住,记 error log
                from app.domain.chat.models import Session

                sid_stmt = select(Session.id).where(Session.child_user_id == child_user_id_val)
                sid_rows = (await child_db.execute(sid_stmt)).scalars().all()
                owned_sids = frozenset(sid_rows)

                today_sessions = (
                    (
                        await child_db.execute(
                            select(Session).where(
                                Session.child_user_id == child_user_id_val,
                                Session.created_at >= day_start,
                                Session.created_at < day_end,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not today_sessions:
                    logger.info(
                        "expert.skip_no_today_session child=%s date=%s",
                        child_user_id_val,
                        report_date,
                    )
                    return
                if len(today_sessions) >= 2:
                    raise RuntimeError(
                        f"child {child_user_id_val} has {len(today_sessions)} sessions "
                        f"on {report_date}, 1:1 invariant violated",
                    )
                today_session_id: uuid.UUID = today_sessions[0].id

                # b. ChildProfile -> ChildProfileSnapshot
                from app.domain.accounts.models import ChildProfile

                child_profile: ChildProfile | None = (
                    (
                        await child_db.execute(
                            select(ChildProfile).where(
                                ChildProfile.child_user_id == child_user_id_val
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if child_profile is None:
                    logger.error(
                        "expert.child_no_profile child=%s",
                        child_user_id_val,
                    )
                    return

                snapshot = ChildProfileSnapshot.from_profile(child_profile)

                # c. crisis_detected_today
                crisis_detected = await _check_crisis_today(
                    child_db,
                    today_session_id,
                )

                # d. dimension_summary（不喂 LLM，仅写 DB）
                dimension_summary = await _aggregate_dimensions(
                    child_db,
                    today_session_id,
                )

                # 构造 ExpertContextSchema
                expert_ctx = ExpertContextSchema(
                    child_user_id=child_user_id_val,
                    owned_session_ids=owned_sids,
                    session_id=today_session_id,
                    report_date=report_date,
                    day_start=day_start,
                    day_end=day_end,
                    dimension_summary=dimension_summary,
                    crisis_detected_today=crisis_detected,
                    max_output_attempts=3,
                    token_budget=settings.expert_token_budget,
                    child_profile=snapshot,
                    settings=settings,
                    db_session_factory=rr.db_session_factory,
                    shared_http_client=rr.shared_http_client,
                )

                # 构造 ExpertGraphState
                state: ExpertGraphState = {
                    "messages": [],
                    "output_attempts": 0,
                    "total_output_tokens": 0,
                    "structured_output": None,
                    "_budget_forced": False,
                }

                # ainvoke 专家图
                await rr.expert_graph.ainvoke(
                    state,
                    context=expert_ctx,  # type: ignore[reportArgumentType]
                    config={
                        "run_name": "daily_report",
                        "metadata": {
                            "child_id": str(child_user_id_val),
                            "report_date": str(report_date),
                        },
                        "tags": ["expert", "daily_report"],
                    },
                )

    results = await asyncio.gather(
        *[_report_for_child(cid) for cid in child_ids],
        return_exceptions=True,
    )

    # 逐项检查结果，异常记日志
    for child_id, result in zip(child_ids, results, strict=False):
        if isinstance(result, Exception):
            logger.error(
                "expert.child_failed child=%s err=%s",
                child_id,
                result,
            )

    logger.info("expert.run_daily_reports done report_date=%s", report_date)
