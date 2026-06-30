"""Expert 域工具编排层。

两个 LangGraph tool handler 函数 + 导出字典。
通过 Runtime DI 注入 context（ExpertContextSchema），
工具 handler 对 LLM 屏蔽异常路径，异常时返回 error ToolMessage。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any

from langchain_core.messages import ToolMessage
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, ProgrammingError, ResourceClosedError

from app.core.time import SHANGHAI
from app.domain.expert.repository import (
    fetch_notes,
    fetch_report,
    fetch_turn_messages,
    search_crisis_topics,
    search_daily_reports,
    search_session_notes,
    search_turn_summaries,
)
from app.domain.expert.schemas import FetchByRefInput, SearchHistoryInput, SearchSourceType

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

    from app.domain.expert.context_schema import ExpertContextSchema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB 异常装饰器
# ---------------------------------------------------------------------------


def _with_db_error_handling(handler):
    """包装 tool handler:DB 错误转 error ToolMessage,代码 bug(ProgrammingError) 照旧上抛。

    Catch 列表依据见 ``backend/scripts/probe_sa_asyncpg_exceptions.py``
    (2026-06-24 跑出的真实故障 → 异常类型映射)。

    设计:
    - ProgrammingError 显式 re-raise(代码 bug,走 stack trace 暴露路径,不通过错误处理守)
    - DBAPIError 基类兜底:覆盖 statement_timeout(抛基类)、InterfaceError(子类)等真实故障
    - ResourceClosedError 单点 catch:use_closed_connection 在 SQLAlchemyError 另一支

    显式 except ProgrammingError: raise 必须放在 DBAPIError 之前,Python except 子句
    按顺序匹配,ProgrammingError 继承自 DBAPIError,放后面会被宽 catch 兜住。
    """

    @wraps(handler)
    async def wrapper(args, runtime, tool_call_id):
        try:
            return await handler(args, runtime, tool_call_id)
        except ProgrammingError:
            # 代码 bug(语法错/表不存在/列错等),走 stack trace 暴露路径
            raise
        except (DBAPIError, ResourceClosedError) as exc:
            logger.exception(
                "expert.tool_handler.db_error tool=%s child=%s type=%s",
                handler.__name__,
                runtime.context.child_user_id,
                type(exc).__name__,
            )
            return ToolMessage(
                content=json.dumps(
                    {
                        "error": "数据库暂时不可用,本次检索失败。"
                        "你可以重试当前 source,或基于已收集的信息生成报告。"
                    },
                    ensure_ascii=False,
                ),
                tool_call_id=tool_call_id,
            )

    return wrapper


# ---------------------------------------------------------------------------
# 工具 handler
# ---------------------------------------------------------------------------


async def _search_history(
    args: dict[str, Any],
    runtime: Runtime[ExpertContextSchema],
    tool_call_id: str,
) -> ToolMessage:
    """检索历史数据(单源)。

    1. 校验 SearchHistoryInput 入参
    2. 确定日期窗口并校验合法性
    3. 按单 source 调对应 repository 函数
    4. 返回 ToolMessage(JSON)
    """
    # ---- 1. Pydantic 校验 ----
    try:
        validated = SearchHistoryInput.model_validate(args)
    except ValidationError as exc:
        payload = {
            "error": "SearchHistoryInput args 校验失败，请按 schema 重发",
            "validation_errors": [
                {
                    "loc": list(err["loc"]),
                    "msg": err["msg"],
                    "type": err["type"],
                }
                for err in exc.errors()
            ],
        }
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            tool_call_id=tool_call_id,
        )

    ctx = runtime.context
    report_date: date = ctx.report_date

    # ---- 2. 日期窗口 ----
    end_date: date = validated.end_date or (report_date - timedelta(days=1))
    start_date: date = validated.start_date or (end_date - timedelta(days=30))

    if start_date > end_date:
        return ToolMessage(
            content=json.dumps(
                {"error": "start_date 不能晚于 end_date"},
                ensure_ascii=False,
            ),
            tool_call_id=tool_call_id,
        )
    if end_date >= report_date:
        return ToolMessage(
            content=json.dumps(
                {"error": "end_date 需在报告日期之前"},
                ensure_ascii=False,
            ),
            tool_call_id=tool_call_id,
        )
    span = (end_date - start_date).days
    if span > 90:
        return ToolMessage(
            content=json.dumps(
                {"error": f"日期间隔 {span} 天，超过允许的 90 天上限"},
                ensure_ascii=False,
            ),
            tool_call_id=tool_call_id,
        )

    # ---- 3. 单源 ----
    source: str = validated.source
    keywords = validated.keywords
    limit = validated.limit
    context_chars = validated.context_chars
    # 转换为带时区的 datetime
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min, SHANGHAI)
    start_dt = datetime.combine(start_date, time.min, SHANGHAI)
    async with ctx.db_session_factory() as db:
        if source == SearchSourceType.TURN_SUMMARY:
            result = await search_turn_summaries(
                db, ctx.child_user_id, keywords, start_dt, end_dt, limit
            )
        elif source == SearchSourceType.SESSION_NOTES:
            result = await search_session_notes(
                db, ctx.child_user_id, keywords, start_dt, end_dt, limit, context_chars
            )
        elif source == SearchSourceType.CRISIS_TOPIC:
            result = await search_crisis_topics(
                db, ctx.child_user_id, keywords, start_dt, end_dt, limit
            )
        elif source == SearchSourceType.DAILY_REPORT:
            result = await search_daily_reports(
                db, ctx.child_user_id, keywords, start_dt, end_dt, limit, context_chars
            )

    return ToolMessage(
        content=json.dumps(
            result,
            ensure_ascii=False,
        ),
        tool_call_id=tool_call_id,
    )


async def _fetch_by_ref(
    args: dict[str, Any],
    runtime: Runtime[ExpertContextSchema],
    tool_call_id: str,
) -> ToolMessage:
    """按 ``(search_source, ref)`` 取对应数据源的完整原文。

    ``search_source`` 取自 ``SearchSourceType``;``ref`` 为该源主键的 UUID
    (由 ``SearchHistoryInput`` 检索结果 ``MatchItem.ref`` 回带)。四种源:
    - turn_summary / crisis_topic → ``fetch_turn_messages`` 取该轮所在上下文窗口
    - session_notes → ``fetch_notes`` 取整段 notes
    - daily_report → ``fetch_report`` 取整条 report

    所有 fetch 均按 ``ctx.child_user_id`` 做行级 join,跨 child 取值会被滤掉。
    """
    # ---- 1. Pydantic 校验 ----
    try:
        validated = FetchByRefInput.model_validate(args)
    except ValidationError as exc:
        payload = {
            "error": "SearchHistoryInput args 校验失败，请按 schema 重发",
            "validation_errors": [
                {
                    "loc": list(err["loc"]),
                    "msg": err["msg"],
                    "type": err["type"],
                }
                for err in exc.errors()
            ],
        }
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            tool_call_id=tool_call_id,
        )

    ref = validated.ref
    source = validated.search_source
    ctx = runtime.context
    context_turns = validated.context_turns

    async with ctx.db_session_factory() as db:
        if source == SearchSourceType.TURN_SUMMARY:
            result = await fetch_turn_messages(db, ctx.child_user_id, source, ref, context_turns)
        elif source == SearchSourceType.CRISIS_TOPIC:
            result = await fetch_turn_messages(db, ctx.child_user_id, source, ref, context_turns)
        elif source == SearchSourceType.SESSION_NOTES:
            result = await fetch_notes(db, ctx.child_user_id, ref)
        elif source == SearchSourceType.DAILY_REPORT:
            result = await fetch_report(db, ctx.child_user_id, ref)
        else:
            # 防御性:SearchSourceType 已穷举,理论上不可达;保留以防 schema 扩展。
            payload = {"error": "search_source 非法"}
            return ToolMessage(
                content=json.dumps(payload, ensure_ascii=False),
                tool_call_id=tool_call_id,
            )

    if result is None:
        payload = {"error": "ref 参数有误或数据源无权限，请检查 ref 值"}
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            tool_call_id=tool_call_id,
        )

    return ToolMessage(
        content=result,
        tool_call_id=tool_call_id,
    )


# ---------------------------------------------------------------------------
# Handler 字典(套 DB 异常装饰器)
# ---------------------------------------------------------------------------

EXPERT_TOOL_HANDLERS: dict[str, Any] = {
    "SearchHistoryInput": _with_db_error_handling(_search_history),
    "FetchByRefInput": _with_db_error_handling(_fetch_by_ref),
}
