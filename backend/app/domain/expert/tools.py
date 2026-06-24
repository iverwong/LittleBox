"""Expert 域工具编排层。

两个 LangGraph tool handler 函数 + 导出字典。
通过 Runtime DI 注入 context（ExpertContextSchema），
工具 handler 对 LLM 屏蔽异常路径，异常时返回 error ToolMessage。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any

from langchain_core.messages import ToolMessage
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, ProgrammingError, ResourceClosedError

from app.domain.expert.repository import (
    fetch_notes,
    fetch_report,
    fetch_turn,
    search_crisis_topics,
    search_daily_reports,
    search_session_notes,
    search_turn_summaries,
)
from app.domain.expert.schemas import FetchByRefInput, SearchHistoryInput

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

    from app.domain.expert.context_schema import ExpertContextSchema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

EXPERT_SEARCH_SOURCE_VALUES: tuple[str, ...] = (
    "turn_summary",
    "session_notes",
    "crisis_topic",
    "daily_report",
)
"""Expert 工具支持的 4 类检索数据源(仅作 Literal 候选)。

LLM 工具入参 ``SearchHistoryInput.source`` 仅接受单值,多源需求由 LLM 多次调用覆盖。
"""

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
# ref 正则
# ---------------------------------------------------------------------------

_REF_NAMED_PATTERN = re.compile(
    r"^(?P<kind_turn>turn):(?P<sid>[0-9a-fA-F-]{36})#(?P<turn>\d+)$"
    r"|^(?P<kind_notes>notes):(?P<nsid>[0-9a-fA-F-]{36})$"
    r"|^(?P<kind_report>report):(?P<rid>[0-9a-fA-F-]{36})$",
)

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
        return ToolMessage(
            content=json.dumps({"error": str(exc)}, ensure_ascii=False),
            tool_call_id=tool_call_id,
        )

    ctx = runtime.context
    child_user_id_str = str(ctx.child_user_id)
    report_date: date = ctx.report_date

    # ---- 2. 日期窗口 ----
    end_date: date = validated.end_date or (report_date - timedelta(days=1))
    start_date: date = validated.start_date or (end_date - timedelta(days=30))

    if start_date > end_date:
        return ToolMessage(
            content=json.dumps(
                {"error": "start_date cannot be after end_date"},
                ensure_ascii=False,
            ),
            tool_call_id=tool_call_id,
        )
    if end_date >= report_date:
        return ToolMessage(
            content=json.dumps(
                {"error": "end_date must be before report_date"},
                ensure_ascii=False,
            ),
            tool_call_id=tool_call_id,
        )
    span = (end_date - start_date).days
    if span > 90:
        return ToolMessage(
            content=json.dumps(
                {"error": f"date span {span}d exceeds maximum 90 days"},
                ensure_ascii=False,
            ),
            tool_call_id=tool_call_id,
        )

    # ---- 3. 单源 ----
    source: str = validated.source
    keywords = validated.keywords
    limit = validated.limit
    context_chars = validated.context_chars

    results: list[dict[str, Any]] = []
    async with ctx.db_session_factory() as db:
        if source == "turn_summary":
            results.extend(
                await search_turn_summaries(
                    db,
                    child_user_id_str,
                    keywords,
                    start_date,
                    end_date,
                    limit,
                    context_chars,
                ),
            )
        elif source == "session_notes":
            results.extend(
                await search_session_notes(
                    db,
                    child_user_id_str,
                    keywords,
                    start_date,
                    end_date,
                    limit,
                    context_chars,
                ),
            )
        elif source == "crisis_topic":
            results.extend(
                await search_crisis_topics(
                    db,
                    child_user_id_str,
                    keywords,
                    start_date,
                    end_date,
                    limit,
                ),
            )
        elif source == "daily_report":
            results.extend(
                await search_daily_reports(
                    db,
                    child_user_id_str,
                    keywords,
                    start_date,
                    end_date,
                    limit,
                    context_chars,
                    exclude_report_date=report_date,
                ),
            )
        else:
            # schema Literal 已收口,此处仅为防御
            return ToolMessage(
                content=json.dumps(
                    {"error": f"unknown source: {source}"},
                    ensure_ascii=False,
                ),
                tool_call_id=tool_call_id,
            )

    # ---- 4. 截断 ----
    results = results[:limit]

    return ToolMessage(
        content=json.dumps(
            {"results": results, "total": len(results)},
            ensure_ascii=False,
        ),
        tool_call_id=tool_call_id,
    )


async def _fetch_by_ref(
    args: dict[str, Any],
    runtime: Runtime[ExpertContextSchema],
    tool_call_id: str,
) -> ToolMessage:
    """按引用键获取完整原文。

    支持三种 ref 格式：
    - turn:{session_id}#{turn}
    - notes:{session_id}
    - report:{report_id}
    """
    # ---- 1. Pydantic 校验 ----
    try:
        validated = FetchByRefInput.model_validate(args)
    except ValidationError as exc:
        return ToolMessage(
            content=json.dumps({"error": str(exc)}, ensure_ascii=False),
            tool_call_id=tool_call_id,
        )

    # ---- 2. 正则解析 ref ----
    ref = validated.ref
    match = _REF_NAMED_PATTERN.match(ref)
    if not match:
        return ToolMessage(
            content=json.dumps(
                {"error": f"invalid ref format: {ref}"},
                ensure_ascii=False,
            ),
            tool_call_id=tool_call_id,
        )

    ctx = runtime.context
    context_turns = validated.context_turns

    # ---- 3. 按 kind 调 repository ----
    async with ctx.db_session_factory() as db:
        # turn:...
        if match.group("kind_turn") is not None:
            sid = match.group("sid")
            turn_str = match.group("turn")
            turn_number = int(turn_str)

            # 所有权校验
            try:
                sid_uuid = uuid.UUID(sid)
            except ValueError:
                return ToolMessage(
                    content=json.dumps(
                        {"error": f"invalid session id format: {sid}"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                )
            if sid_uuid not in ctx.owned_session_ids:
                return ToolMessage(
                    content=json.dumps(
                        {"error": f"session {sid} not owned by child"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                )

            bundle = await fetch_turn(db, sid, turn_number, context_turns)
            if bundle is None:
                return ToolMessage(
                    content=json.dumps(
                        {"error": f"turn {turn_number} in session {sid} not found"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                )

        # notes:...
        elif match.group("kind_notes") is not None:
            sid = match.group("nsid")

            # 所有权校验
            try:
                sid_uuid = uuid.UUID(sid)
            except ValueError:
                return ToolMessage(
                    content=json.dumps(
                        {"error": f"invalid session id format: {sid}"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                )
            if sid_uuid not in ctx.owned_session_ids:
                return ToolMessage(
                    content=json.dumps(
                        {"error": f"session {sid} not owned by child"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                )

            bundle = await fetch_notes(db, sid)
            if bundle is None:
                return ToolMessage(
                    content=json.dumps(
                        {"error": f"notes for session {sid} not found"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                )

        # report:...
        elif match.group("kind_report") is not None:
            rid = match.group("rid")
            try:
                bundle = await fetch_report(db, rid)
            except ValueError as exc:
                return ToolMessage(
                    content=json.dumps(
                        {"error": str(exc)},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                )
            if bundle is None:
                return ToolMessage(
                    content=json.dumps(
                        {"error": f"report {rid} not found"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                )
            # Owner check:report 必须属于 ctx.child_user_id
            if bundle["child_user_id"] != str(ctx.child_user_id):
                return ToolMessage(
                    content=json.dumps(
                        {"error": f"report {rid} not owned by child"},
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                )

        else:
            # 理论上不会走到这里（正则已校验）
            return ToolMessage(
                content=json.dumps(
                    {"error": f"unexpected ref kind in {ref}"},
                    ensure_ascii=False,
                ),
                tool_call_id=tool_call_id,
            )

    # ---- 4. 返回结果 ----
    return ToolMessage(
        content=json.dumps(bundle, default=str, ensure_ascii=False),
        tool_call_id=tool_call_id,
    )


# ---------------------------------------------------------------------------
# Handler 字典(套 DB 异常装饰器)
# ---------------------------------------------------------------------------

EXPERT_TOOL_HANDLERS: dict[str, Any] = {
    "SearchHistoryInput": _with_db_error_handling(_search_history),
    "FetchByRefInput": _with_db_error_handling(_fetch_by_ref),
}
