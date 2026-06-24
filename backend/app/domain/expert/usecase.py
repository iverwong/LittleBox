"""Expert 域写入路径: daily_reports upsert（单事务，独立事务边界）。

``write_expert_results`` 由 expert graph 的 ``write_results`` 节点调用，
在独立事务边界内完成。使用 SQLAlchemy 方言 ``on_conflict_do_update``
实现 PostgreSQL upsert。
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy.dialects.postgresql import insert

from app.domain.expert.models import DailyReport
from app.domain.expert.schemas import ExpertReportSchema

logger = logging.getLogger("expert.db")


async def write_expert_results(
    db,
    child_user_id: uuid.UUID,
    session_id: uuid.UUID,
    report_date: date,
    output: ExpertReportSchema,
    dimension_summary: dict,
) -> None:
    """Upsert daily_reports：INSERT 或 ON CONFLICT 覆盖更新。

    每孩子每天最多一条报告，``(child_user_id, report_date)``
    为唯一索引。重复触发（cron 重跑等）时幂等覆盖。

    6 段内容直接写入独立 Text 列,避免 markdown 拼接 + parse 回路。session_id
    同步进入 SET 子句,即便 1:1 被破坏,重跑时旧 session 与 report 脱钩。

    Args:
        db: 外部传入的 AsyncSession；不负责 commit/close，由调用方管理事务边界。
        child_user_id: 被分析孩子的 user_id。
        session_id: 当日 chat session id。
        report_date: 报告对应的逻辑日。
        output: LLM 产出的 ExpertReportSchema（含 overall_status、degraded、6 段正文）。
        dimension_summary: 代码预聚合的 6 维聚合 dict（不进 LLM，直接写 DB）。
    """
    stmt = insert(DailyReport).values(
        child_user_id=child_user_id,
        session_id=session_id,
        report_date=report_date,
        overall_status=output.overall_status,
        dimension_summary=dimension_summary,
        today_overview=output.today_overview,
        what_was_discussed=output.what_was_discussed,
        emotion_changes=output.emotion_changes,
        noteworthy=output.noteworthy,
        suggestions=output.suggestions,
        anomaly_periods=output.anomaly_periods,
        degraded=output.degraded,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[DailyReport.child_user_id, DailyReport.report_date],
        set_={
            DailyReport.session_id: stmt.excluded.session_id,
            DailyReport.overall_status: stmt.excluded.overall_status,
            DailyReport.dimension_summary: stmt.excluded.dimension_summary,
            DailyReport.today_overview: stmt.excluded.today_overview,
            DailyReport.what_was_discussed: stmt.excluded.what_was_discussed,
            DailyReport.emotion_changes: stmt.excluded.emotion_changes,
            DailyReport.noteworthy: stmt.excluded.noteworthy,
            DailyReport.suggestions: stmt.excluded.suggestions,
            DailyReport.anomaly_periods: stmt.excluded.anomaly_periods,
            DailyReport.degraded: stmt.excluded.degraded,
        },
    )
    await db.execute(stmt)

    logger.info(
        "expert.upsert child=%s session=%s date=%s status=%s degraded=%s",
        child_user_id,
        session_id,
        report_date,
        output.overall_status.value,
        output.degraded,
    )
