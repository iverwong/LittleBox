"""审查结果写入路径:audit_records INSERT + rolling_summaries upsert(单事务)。

`write_audit_results` 由 audit graph 的 `write_results` 节点调用,
或在 ARQ worker 直接调用路径中使用(独立事务边界)。

执行顺序:
1. SELECT FOR UPDATE rolling_summaries
2. 若 rs 存在且 turn <= rs.last_turn → WARN + return(零净写入)
3. INSERT audit_records
4. rs is None → INSERT 新 rs / 存在 → UPDATE(append + crisis_locked OR + last_turn)

时序约束:
- 全程 ORM,批处理至事务提交时落盘
- crisis_locked 累积:一旦为 true 不可回 false
- WHERE last_turn < :turn 防回退(代码层提前 return,不等 DB)
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.core.enums import NotificationType
from app.core.time import now_utc
from app.domain.audit.models import AuditRecord, RollingSummary
from app.domain.audit.schemas import AuditOutputSchema, TurnSummaryEntry

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
    """单事务:INSERT audit_records + upsert rolling_summaries。

    Args:
        db: 外部传入的 AsyncSession;不负责 commit/close,由调用方管理事务边界。
        session_id: 被审查对话 session 的 ID(字符串或 UUID)。
        turn_number: 本轮 ai_turn 编号。
        structured_output: 审查 LLM 输出的结构化结论。
        session_notes_final: 本轮审查维护后的最终 session_notes 全文。
        turn_summary: 本轮客观中立短摘要(≤100 字符)。
        target_message_id: 本轮审查锚点 ai_msg id,首次触发 crisis 时写入
            rolling_summaries.crisis_locked_message_id。
    """
    sid = uuid.UUID(session_id) if isinstance(session_id, str) else session_id

    # SELECT FOR UPDATE rolling_summaries(锁行,防并发 upsert 互相覆盖)
    result = await db.execute(
        select(RollingSummary).where(RollingSummary.session_id == sid).with_for_update(),
    )
    rs = result.scalar_one_or_none()

    # 防回退检查:必须在 INSERT 之前;rs.last_turn 已 ≥ 本轮说明是旧消息回放,跳过
    if rs is not None and turn_number <= rs.last_turn:
        logger.warning(
            "audit.turn.rollback sid=%s turn=%d last_turn=%d",
            sid,
            turn_number,
            rs.last_turn,
        )
        return

    # INSERT audit_records
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
            guidance_injection=structured_output.guidance_injection,
            notify_sent=False,
        )
    )

    # upsert rolling_summaries
    entry = TurnSummaryEntry(
        turn_number=turn_number,
        summary=turn_summary,
        created_at=now_utc().isoformat(),
    )
    entry_dict = entry.model_dump()

    if rs is None:
        # 首次写入:INSERT 新行,crisis_locked_message_id 仅在初始命中时写
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
        # 既有行:append turn_summaries + 更新 last_turn + 视情况设 crisis_locked,
        # crisis_locked_message_id 短路保留旧值(粘性不可逆)
        summaries = (rs.turn_summaries or []) + [entry_dict]
        rs.turn_summaries = summaries
        rs.last_turn = turn_number
        if rs.crisis_locked_message_id is None and structured_output.crisis_detected:
            rs.crisis_locked_message_id = target_message_id
        rs.session_notes = session_notes_final

    # crisis 通知桩:抽到 domain/notifications/notify_stub.send,
    # 日志格式 "notify.stub.<type> sid=<sid> turn=<turn> target=<target>"。
    if structured_output.crisis_detected:
        from app.domain.notifications.notify_stub import send as notify_send

        notify_send(NotificationType.crisis, sid, turn_number, target_message_id)
