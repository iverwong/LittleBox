"""审查结果写入路径(D-3,Phase 4.6):audit_records INSERT + rolling_summaries upsert(单事务)。

D-3 决议:`app/audit/writers.py` 合并到本文件。D-4A.3 落点:严格按 §新目录树
落到 `app/domain/audit/usecase.py`,旧 `app/audit/` 目录暂留 graph/llm/worker/
prompts/context_schema,Phase 6 整体重命名前的过渡形态。

``write_audit_results`` 由 Step 5 audit graph 的 ``write_results`` 节点调用,
或由 ARQ worker ``run_audit`` 直接调用(Step 7)。

执行顺序(必修 2 — 模板 A v2 确认):
1. SELECT FOR UPDATE rolling_summaries
2. 若 rs 存在且 turn <= rs.last_turn → WARN + return(零净写入)
3. INSERT audit_records
4. rs is None → INSERT 新 rs / 存在 → UPDATE(append + crisis_locked OR + last_turn)

时序约束(D13 决议):
- 全程 ORM,批处理至事务提交时落盘
- crisis_locked 累积:一旦为 true 不可回 false
- WHERE last_turn < :turn 防回退(代码层提前 return,不等 DB)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.domain.audit.schemas import AuditOutputSchema, TurnSummaryEntry

# 4.6 期间:app/models/audit.py 仍存在(Phase 6.4 才迁到 app/domain/audit/models.py)
# 故保留 app.models.audit import,Phase 6.4 收口时同步改
from app.models.audit import AuditRecord, RollingSummary

logger = logging.getLogger("audit.db")


async def write_audit_results(
    db,
    session_id: str,
    turn_number: int,
    structured_output: AuditOutputSchema,
    session_notes_final: str,
    turn_summary: str,
    target_message_id: uuid.UUID | None = None,
) -> None:
    """单事务:INSERT audit_records + upsert rolling_summaries。"""
    sid = uuid.UUID(session_id) if isinstance(session_id, str) else session_id

    # Step 1: SELECT FOR UPDATE rolling_summaries
    result = await db.execute(
        select(RollingSummary).where(RollingSummary.session_id == sid).with_for_update(),
    )
    rs = result.scalar_one_or_none()

    # Step 2: 防回退检查(必须在 INSERT 之前)
    if rs is not None and turn_number <= rs.last_turn:
        logger.warning(
            "audit.turn.rollback sid=%s turn=%d last_turn=%d",
            sid,
            turn_number,
            rs.last_turn,
        )
        return

    # Step 3: INSERT audit_records
    dims = (
        structured_output.dimension_scores.model_dump()
        if structured_output.dimension_scores is not None
        else None
    )
    db.add(
        AuditRecord(
            session_id=sid,
            turn_number=turn_number,
            target_message_id=target_message_id,
            dimension_scores=dims,
            crisis_detected=structured_output.crisis_detected,
            crisis_topic=structured_output.crisis_topic,
            guidance_injection=structured_output.guidance_injection,  # schema→ORM 同名透传
            redline_triggered=structured_output.redline_triggered,
            redline_detail=structured_output.redline_detail,
            notify_sent=False,  # M8 期不发送通知;server_default 不足,ORM 需显式
        )
    )

    # Step 4: upsert rolling_summaries
    entry = TurnSummaryEntry(
        turn_number=turn_number,
        summary=turn_summary,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    entry_dict = entry.model_dump()

    if rs is None:
        # 4a: INSERT 新行(crisis_locked_message_id 短路保留:初始命中才写)
        db.add(
            RollingSummary(
                session_id=sid,
                last_turn=turn_number,
                crisis_locked_message_id=(
                    target_message_id if structured_output.crisis_detected else None
                ),
                session_notes=session_notes_final,
                turn_summaries=[entry_dict],
            )
        )
    else:
        # 4b: UPDATE 既有行(crisis_locked_message_id 短路保留旧值,粘性不可逆)
        summaries = (rs.turn_summaries or []) + [entry_dict]
        rs.turn_summaries = summaries
        rs.last_turn = turn_number
        if rs.crisis_locked_message_id is None and structured_output.crisis_detected:
            rs.crisis_locked_message_id = target_message_id
        rs.session_notes = session_notes_final

    # F.4 notifications stub(M10+ 替换为真实推送)
    # D-5 决议(D-4A.3 落地):通知桩抽到 domain/notifications/notify_stub.py。
    # 日志格式 "notify.stub.<type> sid=<sid> turn=<turn> target=<target>"
    # 必须与原 logger.info 字面 byte 级一致,关注点 6 硬约束。
    if structured_output.crisis_detected or structured_output.redline_triggered:
        notify_type = "crisis" if structured_output.crisis_detected else "redline"
        from app.domain.notifications.notify_stub import send as notify_send

        notify_send(notify_type, sid, turn_number, target_message_id)
