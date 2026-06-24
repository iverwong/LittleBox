"""ARQ job function run_audit + 失败标记。

Worker 配置已迁至 app/worker.py；本模块只做 job function。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("audit.worker")

MAX_TRIES = 3


async def run_audit(
    ctx: dict[str, Any],
    sid: str,
    turn_number: int,
    child_user_id: str,
    target_message_id: str,
    child_profile: dict,  # 入队 asdict,出队重建实例
) -> None:
    """执行一次审查(ARQ job function)。

    ARQ 约定:job function 的第一个参数是 ctx dict,之后为自定义参数。
    ctx 包含 RuntimeResources(on_startup 构造) + settings + signals_manager。

    失败处理:
    - 成功 → set_ready
    - 失败 + 还有重试机会 → raise(触发 ARQ retry)
    - 失败 + 已到 max_tries → set_failed + raise(ARQ 会 dead-letter)

    Args:
        ctx: ARQ worker ctx dict(读取 resources / signals_manager / job_try)。
        sid: 被审查对话 session ID(字符串形式)。
        turn_number: 本轮 ai_turn 编号。
        child_user_id: 被审查的青少年用户 ID(字符串形式);由 enqueue_audit
            从 ChatContextSchema 下传,避免 worker 内 SELECT 反查。
        target_message_id: 本轮审查锚点 ai_msg id(字符串形式)。
        child_profile: 入队时通过 asdict 序列化的 ChildProfileSnapshot,
            出队时通过 ChildProfileSnapshot(**child_profile) 重建。
    """
    import uuid

    from app.core.runtime import RuntimeResources
    from app.domain.audit.context_schema import AuditContextSchema

    rr: RuntimeResources = ctx["resources"]
    manager = ctx["signals_manager"]
    # 重建,确保新增字段通过默认值传入,而非报错
    from app.domain.accounts.schemas import ChildProfileSnapshot

    snapshot = ChildProfileSnapshot(**child_profile)

    try:
        audit_ctx = AuditContextSchema(
            session_id=uuid.UUID(sid),
            child_user_id=uuid.UUID(child_user_id),
            target_message_id=uuid.UUID(target_message_id),
            max_iter=rr.settings.max_audit_tool_iterations,
            child_profile=snapshot,
            settings=rr.settings,
            db_session_factory=rr.db_session_factory,
            audit_redis=rr.audit_redis,
            shared_http_client=rr.shared_http_client,
        )
        state: dict[str, Any] = {
            "sid": sid,
            "turn_number": turn_number,
            "session_notes_working": "",
            "tool_iter_count": 0,
            "structured_output": None,
            "messages": [],
        }
        result: dict[str, Any] = await rr.audit_graph.ainvoke(
            state,
            context=audit_ctx,  # type: ignore[reportArgumentType]
            config={
                "run_name": "audit",
                "metadata": {
                    "session_id": str(audit_ctx.session_id),
                    "child_id": str(audit_ctx.child_user_id),
                    "turn_number": turn_number,
                    "target_message_id": str(audit_ctx.target_message_id),
                },
                "tags": ["audit"],
            },
        )
        output = result.get("structured_output")
        if output is not None:
            await manager.set_ready(
                sid,
                turn_number,
                output,
                completed_at=datetime.now(UTC).isoformat(),
            )
        else:
            raise RuntimeError(f"audit output is None sid={sid} turn={turn_number}")
    except Exception as e:
        job_try: int = ctx.get("job_try", 1)
        if job_try >= MAX_TRIES:
            await manager.set_failed(
                sid,
                turn_number,
                str(e),
                completed_at=datetime.now(UTC).isoformat(),
            )
        raise
