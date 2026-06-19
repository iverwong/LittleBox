"""通知桩:audit 域危机通知的最小写入实现。

调用方位于 `app/domain/audit/usecase.py`,在 `crisis_detected` 为真时通过
`from app.domain.notifications.notify_stub import send as notify_send` 拉起本函数。

当前仅写一条结构化日志,不做真实推送;替换为真实通道(APNs / 极光 / 微信模板消息
等)时,只改本文件 `send()` 实现,不动调用方。

logger 名保持 `"audit.db"`,与 `audit/usecase.py` 同名 logger 一致,不切换日志输出通道。
"""

from __future__ import annotations

import logging
import uuid

from app.core.enums import NotificationType

logger = logging.getLogger("audit.db")


def send(
    notify_type: NotificationType,
    session_id: uuid.UUID,
    turn_number: int,
    target_message_id: uuid.UUID | None,
) -> None:
    """写入通知桩日志。

    Args:
        notify_type: 通知类型枚举。
        session_id: 触发通知的 session UUID。
        turn_number: 触发通知的 turn 号。
        target_message_id: 触发通知的目标 message UUID(可空)。

    Returns:
        None。

    日志格式 `"notify.stub.<type> sid=<sid> turn=<turn> target=<target>"`
    被 `tests/audit/test_writers.py` 中的 `test_notify_stub_*` 系列用例断言,
    字段顺序与占位符需保持字面一致。
    """
    logger.info(
        "notify.stub.%s sid=%s turn=%d target=%s",
        notify_type.value,
        session_id,
        turn_number,
        target_message_id,
    )
