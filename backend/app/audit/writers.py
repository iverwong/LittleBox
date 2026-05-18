"""审查结果写入路径：audit_records INSERT + rolling_summaries upsert（单事务）。

``write_audit_results`` 由 Step 5 audit graph 的 ``write_results`` 节点调用，
或由 ARQ worker ``run_audit`` 直接调用（Step 7）。

时序约束（D13 决议）：
- audit_records 每次 INSERT 一行
- rolling_summaries SELECT FOR UPDATE 读改写 upsert
- ``WHERE last_turn < :turn`` 防回退
- ``crisis_locked`` 累积语义：一旦为 true 不可回 false
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.schemas.audit import AuditOutputSchema, TurnSummaryEntry


async def write_audit_results(
    db,
    session_id: str,
    turn_number: int,
    structured_output: AuditOutputSchema,
    session_notes_final: str,
    turn_summary: str,
) -> None:
    """单事务：INSERT audit_records + upsert rolling_summaries。"""
    sid = uuid.UUID(session_id) if isinstance(session_id, str) else session_id

    # 1. INSERT audit_records
    dims_json = None
    if structured_output.dimension_scores is not None:
        dims_json = json.dumps(structured_output.dimension_scores.model_dump())

    await db.execute(
        text("""
            INSERT INTO audit_records
                (session_id, turn_number, dimension_scores, crisis_detected, crisis_topic,
                 guidance_injection, redline_triggered, redline_detail)
            VALUES
                (:session_id, :turn_number, CAST(:dimension_scores AS jsonb), :crisis_detected, :crisis_topic,
                 :guidance, :redline_triggered, :redline_detail)
        """),
        {
            "session_id": sid,
            "turn_number": turn_number,
            "dimension_scores": dims_json,
            "crisis_detected": structured_output.crisis_detected,
            "crisis_topic": structured_output.crisis_topic,
            "guidance": structured_output.guidance,
            "redline_triggered": structured_output.redline_triggered,
            "redline_detail": structured_output.redline_detail,
        },
    )

    # 2. Build turn summary entry
    entry = TurnSummaryEntry(
        turn_number=turn_number,
        summary=turn_summary,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    # 3. SELECT FOR UPDATE rolling_summaries
    row = await db.execute(
        text("""
            SELECT id, last_turn, crisis_locked, turn_summaries
            FROM rolling_summaries
            WHERE session_id = :session_id
            FOR UPDATE
        """),
        {"session_id": sid},
    )
    existing = row.fetchone()

    if existing is None:
        # 不存在 → INSERT 新行
        await db.execute(
            text("""
                INSERT INTO rolling_summaries
                    (session_id, last_turn, crisis_locked, session_notes, turn_summaries)
                VALUES
                    (:session_id, :turn_number, :crisis_locked, :session_notes, CAST(:turn_summaries AS jsonb))
            """),
            {
                "session_id": sid,
                "turn_number": turn_number,
                "crisis_locked": structured_output.crisis_detected,
                "session_notes": session_notes_final,
                "turn_summaries": json.dumps([entry.model_dump()]),
            },
        )
    else:
        # 存在 → UPDATE：串联 turn_summaries + 累积 crisis_locked + 覆盖 session_notes
        if turn_number <= existing.last_turn:
            # WHERE last_turn < :turn 防回退（被 DB 层兜底，代码层也提前返回）
            return

        existing_summaries = existing.turn_summaries or []
        existing_summaries.append(entry.model_dump())
        new_crisis_locked = existing.crisis_locked or structured_output.crisis_detected

        await db.execute(
            text("""
                UPDATE rolling_summaries
                SET last_turn = :turn_number,
                    crisis_locked = :crisis_locked,
                    session_notes = :session_notes,
                    turn_summaries = CAST(:turn_summaries AS jsonb)
                WHERE id = :id
            """),
            {
                "id": existing.id,
                "turn_number": turn_number,
                "crisis_locked": new_crisis_locked,
                "session_notes": session_notes_final,
                "turn_summaries": json.dumps(existing_summaries),
            },
        )
