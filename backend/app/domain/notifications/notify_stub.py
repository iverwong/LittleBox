"""通知桩(D-5,Phase 4.5):audit 域危机/红线通知的占位实现。

M10+ 替换为真实推送(APNs / 极光 / 微信模板消息等)时,只改本文件 send()
实现,不动调用方(audit/usecase.py)。

D-4A.3 决议:logger 名保持 "audit.db",与原 audit/writers.py 末尾
logger.info("notify.stub...") 一致 — 不切换日志输出通道,audit worker
日志聚合路径不变。
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger("audit.db")


def send(
    notify_type: str,
    session_id: uuid.UUID,
    turn_number: int,
    target_message_id: uuid.UUID | None,
) -> None:
    """发送通知桩(占位实现)。

    Args:
        notify_type: "crisis" 或 "redline"
        session_id:  触发通知的 session UUID
        turn_number: 触发通知的 turn 号
        target_message_id: 触发通知的目标 message UUID(可空)

    日志格式 "notify.stub.<type> sid=<sid> turn=<turn> target=<target>"
    必须与原 logger.info("notify.stub.%s sid=%s turn=%d target=%s", ...)
    字面 byte 级一致,关注点 6 硬约束。
    """
    logger.info(
        "notify.stub.%s sid=%s turn=%d target=%s",
        notify_type,
        session_id,
        turn_number,
        target_message_id,
    )
