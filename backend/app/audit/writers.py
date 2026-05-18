"""审查结果写入路径（M8 Step 6 替换实体）。

当前为 forward-reference stub，Step 6 落地 real INSERT + upsert。
"""
from __future__ import annotations

from app.schemas.audit import AuditOutputSchema


async def write_audit_results(
    db,
    session_id: str,
    turn_number: int,
    structured_output: AuditOutputSchema,
    session_notes_final: str,
    turn_summary: str,
) -> None:
    """写入 audit_records + rolling_summaries（单事务）。

    TODO(M8 Step 6): 替换为真实 INSERT audit_records + SELECT FOR UPDATE upsert rolling_summaries。
    """
    raise NotImplementedError("Step 6: replace with real INSERT + upsert")
