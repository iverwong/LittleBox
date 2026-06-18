"""chat 域 usecase:跨表事务 / 跨外部服务(DB + Redis + arq)的事务编排。

D-2 边界:
- 装:跨表事务(单条 AI 消息 INSERT + sessions.ai_turn_counter 列自增)、
  跨外部服务(Redis 信号管道 + arq 任务队列)的事务编排
- 不装:LangGraph 节点 / 路由(放 graph.py)
- 不装:HTTP 协议层(放 me.py 路由 handler)
- 不装:纯算法(放 chat 域内对应模块)

跨域 import 边界债(本期 verbatim 保留,D-3A.3 登记):
- `enqueue_audit` 内部用 `app.domain.audit.signals.AuditSignalsManager`
  (audit 域),`usecase.py`(chat 域)→ audit 域反向引用,
  pre-existing 耦合,本 Phase 3 不修,等 Phase 4.x 域通信重构。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import InterventionType, MessageRole, MessageStatus
from app.domain.accounts.schemas import ChildProfileSnapshot
from app.domain.audit.signals import AuditSignalsManager
from app.domain.chat.models import Message, Session

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 跨域契约常量(G3-6 收口)
# ---------------------------------------------------------------------------

# ⚠️ 此字面量必须与 worker.py WORKER_SETTINGS["functions"] 字符串路径逐字一致。
# arq 0.28 的 func() 对字符串路径使用 name=name or coroutine(全路径做函数名 key);
# 若两侧不匹配,worker 日志 "function '<name>' not found",job 永不消费。
# 移动或重命名 worker 模块时必须同步更新此字面量。
#
# 跨域常量化:worker.py 属 audit 域,遵 D-1 不 import chat usecase,因此两侧
# 字面量在各自模块独立维护。`tests/integration/chat/test_contract_audit_job_name`
# 实质断言两侧相等(从 usecase.py 与 worker.py 各取一次字面量)。
AUDIT_JOB_NAME = "app.domain.audit.worker.run_audit"


# ---------------------------------------------------------------------------
# 跨表事务:persist_ai_turn
# ---------------------------------------------------------------------------


async def persist_ai_turn(
    db: AsyncSession,
    sid: uuid.UUID,
    finish_reason: str,
    content: str,
    turn_number: int,
    intervention_type: InterventionType | None = None,
) -> uuid.UUID:
    """持久化一条 AI 消息行 + 同事务自增 ai_turn_counter（M9-patch1 单写点收敛）。

    收敛后：me.py 的两个写行分支（StopWithAi / 自然结束）统一调此函数，
    不再内联手搓 Message。调用方负责 usage_meta 记账和 enqueue_audit。

    last_active_at 由 commit① 独占，本函数不覆写（F 决策 / M6-patch3）。

    Args:
        db: async DB session
        sid: session UUID
        finish_reason: LLM stop reason (stop / length / content_filter / user_stopped)
        content: accumulated text content
        turn_number: 当前轮号（commit① human + commit② ai 共享同号）
        intervention_type: None=normal, crisis/guided

    Returns:
        The id of the newly inserted AI message row (uuid.UUID).
    """
    msg = Message(
        session_id=sid,
        role=MessageRole.ai,
        content=content,
        status=MessageStatus.active,
        finish_reason=finish_reason,
        turn_number=turn_number,
        intervention_type=intervention_type,
    )
    db.add(msg)
    await db.flush()  # populate msg.id
    # M8: ai_turn_counter 同事务 +1（SQL 列表达式，PG 行锁安全）
    await db.execute(
        update(Session).where(Session.id == sid).values(ai_turn_counter=Session.ai_turn_counter + 1)
    )
    return msg.id


# ---------------------------------------------------------------------------
# 跨外部服务:enqueue_audit
# ---------------------------------------------------------------------------


async def enqueue_audit(
    arq_pool: "ArqRedis",
    audit_redis: "Redis",
    sid: uuid.UUID,
    db: AsyncSession,
    turn_number: int,
    child_user_id: uuid.UUID,
    target_message_id: uuid.UUID,
    child_profile: ChildProfileSnapshot,
) -> None:
    """SET Redis pending + ARQ enqueue 触发异步审查。

    入队使用 asdict 确保签名变更不会影响原有job
    """
    manager = AuditSignalsManager(audit_redis, ttl=settings.audit_redis_ttl_seconds)
    await manager.set_pending(str(sid), turn_number, started_at=datetime.now(UTC).isoformat())

    await arq_pool.enqueue_job(
        AUDIT_JOB_NAME,
        str(sid),
        turn_number,
        str(child_user_id),
        str(target_message_id),
        asdict(child_profile),
        _job_id=f"audit:{sid}:{turn_number}",
    )

    logger.info("audit.enqueued sid=%s turn=%s", sid, turn_number)
