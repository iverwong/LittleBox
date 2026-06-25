"""Expert 图 per-run 不可变上下文(Runtime[ExpertContextSchema])。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.accounts.schemas import ChildProfileSnapshot

if TYPE_CHECKING:
    from datetime import date, datetime

    import httpx

    from app.core.config import Settings


@dataclass(frozen=True)
class ExpertContextSchema:
    """专家图单次运行的不可变上下文。

    与 RuntimeResources(进程级)的分工:RuntimeResources 承载容器级共享资源
    (engine / pool / shared_http_client / CompiledStateGraph 等),
    ExpertContextSchema 承载单次图调用所需的请求级上下文。二者均 frozen=True,
    运行时不可变。

    Attributes:
        child_user_id: 被分析的青少年用户 ID。
        owned_session_ids: 该孩子所有 session ID 白名单(建图前一次性查出),
            用于工具 handler 内存校验。
        session_id: 当日 chat session,expert 锚定目标,worker 层按 day_start/day_end
            过滤 owned_session_ids 唯一取一条。
        report_date: 刚结束的逻辑日(logical_day(now, boundary_hour=4) - 1day)。
        day_start: 逻辑日窗口起始时间(tz-aware,带时区)。
            worker 层一次性算好,load_context 直接取用,避免与 worker 重复计算。
        day_end: 逻辑日窗口结束时间(tz-aware,带时区)。
        dimension_summary: 代码预聚合的 6 维 peak/mean/high_ratio,
            不喂 LLM,write_results 节点直接写入 DB。
        crisis_detected_today: 当日逻辑窗口内是否有任一 crisis_detected=True,
            用于 overall_status 地板判定。
        max_output_attempts: ExpertReportSchema 调用上限,默认 3。
        token_budget: 资料收集 token 预算,默认 100_000;累计 LLM 输出 token
            超限时注入强制交卷 HumanMessage。
        child_profile: 孩子档案快照(用于 prompt 注入)。
        settings: 应用配置。
        db_session_factory: DB 会话工厂,worker 层负责注入。
        shared_http_client: 进程级共享 httpx 客户端,worker 层从
            rr.shared_http_client 注入。
    """

    # 身份字段
    child_user_id: uuid.UUID  # 被分析的青少年用户 ID
    owned_session_ids: frozenset[uuid.UUID]  # 该孩子所有 session ID 白名单
    session_id: uuid.UUID  # 当日 chat session,expert 锚定目标
    # 业务字段
    report_date: date  # 刚结束的逻辑日
    day_start: datetime  # 逻辑日窗口起始(tz-aware)
    day_end: datetime  # 逻辑日窗口结束(tz-aware)
    dimension_summary: dict  # 代码预聚合的 6 维聚合(不喂 LLM)
    crisis_detected_today: bool  # 当日是否有 crisis 标记
    max_output_attempts: int  # ExpertReportSchema 调用上限
    token_budget: int  # 资料收集 token 预算
    child_profile: ChildProfileSnapshot  # 孩子档案快照
    # 三资源
    settings: Settings  # 应用配置
    db_session_factory: async_sessionmaker[AsyncSession]  # DB 会话工厂
    shared_http_client: httpx.AsyncClient  # 进程级共享 httpx 客户端
