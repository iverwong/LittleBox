"""Expert 域写入路径: daily_reports upsert(单事务，独立事务边界)。

`write_expert_results` 由 expert graph 的 `write_results` 节点调用，
在独立事务边界内完成。使用原始 SQL ON CONFLICT upsert。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text

from app.domain.expert.schemas import ExpertReportSchema

logger = logging.getLogger("expert.db")


async def write_expert_results(
    db,
    child_user_id,
    report_date,
    output: ExpertReportSchema,
    dimension_summary: dict,
) -> None:
    """原始 SQL upsert:INSERT daily_reports → ON CONFLICT DO UPDATE。

    content 字段将 ExpertReportSchema 的 6 段内容拼接为 markdown 格式。

    Args:
        db: 外部传入的 AsyncSession;不负责 commit/close,由调用方管理事务边界。
        child_user_id: 被分析孩子的 user_id。
        report_date: 报告对应的自然日。
        output: LLM 产出的 ExpertReportSchema(含 overall_status、degraded、6 段正文)。
        dimension_summary: 代码预聚合的 6 维聚合 dict(不进 LLM,直接写 DB)。
    """
    content = (
        f"## 今日概览\n\n{output.today_overview}\n\n"
        f"## 聊了什么\n\n{output.what_was_discussed}\n\n"
        f"## 情绪变化\n\n{output.emotion_changes}\n\n"
        f"## 值得关注\n\n{output.noteworthy}\n\n"
        f"## 具体建议\n\n{output.suggestions}\n\n"
        f"## 异常时段标注\n\n{output.anomaly_periods}"
    )
    # 注：使用原始 SQL 因为 PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE``
    # 是 PostgreSQL 专有语法，SQLAlchemy ORM 的 merge() 不适用于此批量 upsert 路径。
    stmt = text("""
        INSERT INTO daily_reports
            (child_user_id, report_date, overall_status, dimension_summary, content, degraded)
        VALUES (:cid, :rdate, :status, :dims, :content, :degraded)
        ON CONFLICT (child_user_id, report_date) DO UPDATE SET
            overall_status = EXCLUDED.overall_status,
            dimension_summary = EXCLUDED.dimension_summary,
            content = EXCLUDED.content,
            degraded = EXCLUDED.degraded
    """)
    await db.execute(
        stmt,
        {
            "cid": child_user_id,
            "rdate": report_date,
            "status": output.overall_status.value,
            "dims": json.dumps(dimension_summary),
            "content": content,
            "degraded": output.degraded,
        },
    )
    logger.info(
        "expert.upsert child=%s date=%s status=%s degraded=%s",
        child_user_id,
        report_date,
        output.overall_status.value,
        output.degraded,
    )
