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
from typing import TYPE_CHECKING, Any

from langchain_core.messages import ToolMessage
from pydantic import ValidationError

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

EXPERT_SEARCH_SOURCES: list[str] = [
    "turn_summary",
    "session_notes",
    "crisis_topic",
    "daily_report",
]
"""Expert 工具支持的 4 类检索数据源。"""

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
    """检索历史数据。

    1. 校验 SearchHistoryInput 入参
    2. 确定日期窗口并校验合法性
    3. 扇出调 repository 各数据源
    4. occurred_at DESC 排序 + limit 截断
    5. 返回 ToolMessage(JSON)
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

    # ---- 3. 确定 sources ----
    sources: list[str] = validated.sources or EXPERT_SEARCH_SOURCES
    keywords = validated.keywords
    limit = validated.limit
    context_chars = validated.context_chars

    # ---- 4. 扇出调 repository ----
    results: list[dict[str, Any]] = []

    async with ctx.db_session_factory() as db:
        if "turn_summary" in sources:
            turn_results = await search_turn_summaries(
                db,
                child_user_id_str,
                keywords,
                start_date,
                end_date,
                limit,
                context_chars,
            )
            results.extend(turn_results)

        if "session_notes" in sources:
            notes_results = await search_session_notes(
                db,
                child_user_id_str,
                keywords,
                start_date,
                end_date,
                limit,
                context_chars,
            )
            results.extend(notes_results)

        if "crisis_topic" in sources:
            crisis_results = await search_crisis_topics(
                db,
                child_user_id_str,
                keywords,
                start_date,
                end_date,
                limit,
            )
            results.extend(crisis_results)

        if "daily_report" in sources:
            daily_results = await search_daily_reports(
                db,
                child_user_id_str,
                keywords,
                start_date,
                end_date,
                limit,
                context_chars,
                exclude_report_date=report_date,
            )
            results.extend(daily_results)

    # ---- 5. 排序 + 截断 ----
    results.sort(key=lambda r: r.get("occurred_at") or "", reverse=True)
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
# Handler 字典
# ---------------------------------------------------------------------------

EXPERT_TOOL_HANDLERS: dict[str, Any] = {
    "SearchHistoryInput": _search_history,
    "FetchByRefInput": _fetch_by_ref,
}
