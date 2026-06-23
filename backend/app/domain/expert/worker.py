"""ARQ cron job: run_daily_reports。

在 04:05（Asia/Shanghai）由 ARQ cron 触发，遍历所有活跃孩子生成日终报告。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.core.time import SHANGHAI, logical_day

logger = logging.getLogger("expert.worker")

# 六维度 key 列表，对齐 AuditDimensionScores / SensitivityConfig
DIMENSIONS = ["emotional", "social", "values", "boundaries", "academic", "lifestyle"]
# 高维分数阈值（>= 此值计为 high_turn）
_HIGH_SCORE_THRESHOLD = 7


async def _check_crisis_today(
    db,
    child_user_id: uuid.UUID,
    day_start: datetime,
    day_end: datetime,
) -> bool:
    """查询当日逻辑窗口内是否有任一 crisis 标记。

    Args:
        db: DB session。
        child_user_id: 孩子用户 ID。
        day_start: 窗口起始时间（带时区）。
        day_end: 窗口结束时间（带时区）。

    Returns:
        True 表示当日有 crisis 标记。
    """
    stmt = text("""
        SELECT EXISTS(
            SELECT 1
            FROM audit_records ar
            JOIN sessions s ON s.id = ar.session_id
            WHERE s.child_user_id = :child_id
              AND ar.crisis_detected = True
              AND ar.created_at >= :start
              AND ar.created_at < :end
        )
    """)
    row = await db.execute(
        stmt,
        {"child_id": child_user_id, "start": day_start, "end": day_end},
    )
    return row.scalar() or False


async def _aggregate_dimensions(
    db,
    owned_session_ids: frozenset[uuid.UUID],
    day_start: datetime,
    day_end: datetime,
) -> dict:
    """从 audit_records 聚合维度的 peak / mean / high_ratio。

    仅查询 dimension_scores IS NOT NULL 的记录。
    对每个维度计算：
      - peak：该维度最大分数
      - mean：该维度平均分数
      - high_ratio：分数 >= 7 的记录数 / 总记录数

    Args:
        db: DB session。
        owned_session_ids: 该孩子所有 session ID 白名单。
        day_start: 窗口起始时间（带时区）。
        day_end: 窗口结束时间（带时区）。

    Returns:
        {dim: {"peak": int, "mean": float, "high_ratio": float}} 格式的 dict。
        无数据时各值为 0。
    """
    sids = list(owned_session_ids)
    if not sids:
        return {d: {"peak": 0, "mean": 0.0, "high_ratio": 0.0} for d in DIMENSIONS}

    stmt = text("""
        SELECT dimension_scores
        FROM audit_records
        WHERE session_id = ANY(:sids)
          AND created_at >= :start
          AND created_at < :end
          AND dimension_scores IS NOT NULL
    """)
    rows = (await db.execute(stmt, {"sids": sids, "start": day_start, "end": day_end})).fetchall()

    dim_scores: dict[str, list[int]] = {d: [] for d in DIMENSIONS}
    for row in rows:
        ds = row[0] or {}
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


def _parse_today_overview_from_content(content: str) -> str:
    """从 markdown content 提取 today_overview 首段。

    content 格式：
        ## 今日概览

        {today_overview}

        ## 聊了什么

    Args:
        content: 完整 markdown 报告 content 字段。

    Returns:
        today_overview 文本；若解析失败则返回 content 前 200 字符。
    """
    try:
        _header = "## 今日概览\n\n"
        if content.startswith(_header):
            remaining = content[len(_header) :]
            next_section = remaining.find("\n\n## ")
            if next_section > 0:
                return remaining[:next_section].strip()
            return remaining.strip()
    except Exception:
        pass
    return content[:200]


async def _get_recent_reports(
    db,
    child_user_id: uuid.UUID,
    report_date,
    days: int = 7,
) -> list[dict]:
    """查询近 days 天的历史每日报告概要。

    Args:
        db: DB session。
        child_user_id: 孩子用户 ID。
        report_date: 当前报告日期（不包含当天）。
        days: 往前查的天数，默认 7。

    Returns:
        list[dict]，每项含 report_date / overall_status / today_overview。
    """
    stmt = text("""
        SELECT report_date::text, overall_status, content
        FROM daily_reports
        WHERE child_user_id = :child_id
          AND report_date < :report_date
          AND report_date >= :min_date
        ORDER BY report_date DESC
    """)
    rows = await db.execute(
        stmt,
        {
            "child_id": child_user_id,
            "report_date": report_date,
            "min_date": report_date - timedelta(days=days),
        },
    )
    result: list[dict] = []
    for row in rows:
        report_date_str: str = str(row[0])
        status: str = str(row[1])
        content: str = str(row[2]) if row[2] else ""
        result.append(
            {
                "report_date": report_date_str,
                "overall_status": status,
                "today_overview": _parse_today_overview_from_content(content),
            }
        )
    return result


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

    report_date = logical_day(datetime.now(UTC), boundary_hour=4) - timedelta(days=1)
    logger.info("expert.run_daily_reports start report_date=%s", report_date)

    # 逻辑日窗口：report_date 4:00 Shanghai -> report_date+1 4:00 Shanghai
    day_start = datetime.combine(report_date, datetime.min.time()).replace(
        tzinfo=SHANGHAI,
    ) + timedelta(hours=4)
    day_end = day_start + timedelta(days=1)

    # 查所有活跃孩子（JOIN ChildProfile 确保存在画像）
    async with rr.db_session_factory() as db:
        child_stmt = text("""
            SELECT u.id
            FROM users u
            JOIN child_profiles cp ON cp.child_user_id = u.id
            WHERE u.role = 'child'
              AND u.is_active = True
        """)
        child_rows = (await db.execute(child_stmt)).fetchall()
        child_ids = [row[0] for row in child_rows]

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
                # a. owned_session_ids
                sid_stmt = text("""
                    SELECT id FROM sessions WHERE child_user_id = :child_id
                """)
                sid_rows = (
                    await child_db.execute(sid_stmt, {"child_id": child_user_id_val})
                ).fetchall()
                owned_sids = frozenset(
                    uuid.UUID(str(r[0])) if not isinstance(r[0], uuid.UUID) else r[0]
                    for r in sid_rows
                )

                # b. ChildProfile -> ChildProfileSnapshot
                prof_stmt = text("""
                    SELECT u.id, cp.nickname, cp.gender::text, cp.birth_date,
                           cp.sensitivity, cp.custom_redlines, cp.concerns
                    FROM child_profiles cp
                    JOIN users u ON u.id = cp.child_user_id
                    WHERE cp.child_user_id = :child_id
                """)
                prof_row = (
                    await child_db.execute(prof_stmt, {"child_id": child_user_id_val})
                ).first()
                if prof_row is None:
                    logger.warning(
                        "expert.child_no_profile child=%s",
                        child_user_id_val,
                    )
                    return

                from app.core.time import age_at

                snapshot = ChildProfileSnapshot(
                    child_user_id=child_user_id_val,
                    nickname=prof_row.nickname,
                    gender=str(prof_row.gender),
                    birth_date=prof_row.birth_date,
                    age=age_at(prof_row.birth_date),
                    sensitivity=prof_row.sensitivity,
                    custom_redlines=prof_row.custom_redlines,
                    concerns=prof_row.concerns,
                )

                # c. crisis_detected_today
                crisis_detected = await _check_crisis_today(
                    child_db,
                    child_user_id_val,
                    day_start,
                    day_end,
                )

                # d. dimension_summary（不喂 LLM，仅写 DB）
                dimension_summary = await _aggregate_dimensions(
                    child_db,
                    owned_sids,
                    day_start,
                    day_end,
                )

                # e. recent_reports_overview
                recent_reports = await _get_recent_reports(
                    child_db,
                    child_user_id_val,
                    report_date,
                    days=7,
                )

                # 构造 ExpertContextSchema
                expert_ctx = ExpertContextSchema(
                    child_user_id=child_user_id_val,
                    owned_session_ids=owned_sids,
                    report_date=report_date,
                    dimension_summary=dimension_summary,
                    recent_reports_overview=recent_reports,
                    crisis_detected_today=crisis_detected,
                    max_output_attempts=3,
                    token_budget=settings.expert_token_budget,
                    child_profile=snapshot,
                    settings=settings,
                    db_session_factory=rr.db_session_factory,
                    shared_http_client=rr.shared_http_client,
                )

                # 构造 ExpertGraphState
                state: dict[str, Any] = {
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
