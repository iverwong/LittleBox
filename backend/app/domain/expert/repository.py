"""expert 域数据源仓储层：ORM 只读查询。

7 个只读函数（4 个 search_* + 3 个 fetch_*）+ 3 个 helper，全部走 ORM
（``select`` + ``.where()``）。仅在 PG 专有语法必需时（本文件当前无）用 ``text()``。

表依赖（只读，不跨域 import ORM 模型）：
- sessions / messages (chat 域)
- rolling_summaries / turn_summaries / audit_records (audit 域)
- daily_reports (expert 域)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit.models import AuditRecord, RollingSummary, TurnSummary
from app.domain.chat.models import Message, MessageRole, MessageStatus, Session
from app.domain.expert.models import DailyReport
from app.domain.expert.schemas import (
    FetchDailyReportResult,
    FetchMessageResult,
    FetchRollingSummaryResult,
    MatchItem,
    SearchResult,
    SearchSourceType,
)


def _escape_like(s: str) -> str:
    """转义 LIKE/ILIKE 通配符 ``\\ % _``,与 SQL 端 ``ESCAPE '\\'`` 配对。

    Args:
        s: 原始关键词。

    Returns:
        转义后的关键词；已对反斜杠 / 百分号 / 下划线做前缀转义。
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# Helper 函数
# ---------------------------------------------------------------------------


def _extract_snippet(
    text: str,
    keywords: list[str],
    context_chars: int,
) -> str:
    """长文本中定位第一个关键字命中，取前后 context_chars 窗口。

    Args:
        text: 原始文本。
        keywords: 关键词列表，OR 匹配。
        context_chars: 上下文窗口字符数。0 则返回匹配行。

    Returns:
        截取后的 snippet 字符串。无匹配则返回 text[:context_chars]。
    """
    text_lower = text.lower()
    best_start = 0
    best_end = 0
    found = False

    for kw in keywords:
        kw_lower = kw.lower()
        idx = text_lower.find(kw_lower)
        if idx != -1 and (not found or idx < best_start):
            best_start = idx
            best_end = idx + len(kw)
            found = True

    if not found:
        return text[:context_chars] if context_chars > 0 else text

    if context_chars == 0:
        # 返回匹配行（按换行符分割）
        line_start = text.rfind("\n", 0, best_start)
        line_end = text.find("\n", best_end)
        if line_start == -1:
            line_start = 0
        if line_end == -1:
            line_end = len(text)
        return text[line_start:line_end].strip()

    snippet_start = max(0, best_start - context_chars)
    snippet_end = min(len(text), best_end + context_chars)

    snippet = text[snippet_start:snippet_end]
    if snippet_start > 0:
        snippet = "..." + snippet
    if snippet_end < len(text):
        snippet = snippet + "..."

    return snippet


async def _get_session_time_range(
    db: AsyncSession,
    session_id: str,
) -> str | None:
    """返回 session 的时间范围字符串，如 ``2026-06-22T09:00:00 ~ 2026-06-22T21:30:00``。

    取该 session 内 messages 表中最早和最晚的 created_at。

    Args:
        db: 异步 DB session。
        session_id: session 的 UUID 字符串。

    Returns:
        格式化的时间范围字符串；若 session 不存在或无消息则返回 None。
    """
    from app.domain.chat.models import Message

    stmt = select(
        func.min(Message.created_at),
        func.max(Message.created_at),
    ).where(Message.session_id == session_id)
    row = (await db.execute(stmt)).one_or_none()
    if row is None or row[0] is None:
        return None
    return "{} ~ {}".format(
        row[0].strftime("%Y-%m-%dT%H:%M:%S"),
        row[1].strftime("%Y-%m-%dT%H:%M:%S"),
    )


# ---------------------------------------------------------------------------
# 主查询函数
# ---------------------------------------------------------------------------


async def search_turn_summaries(
    db: AsyncSession,
    child_user_id: UUID,
    keywords: list[str],
    start: datetime,
    end: datetime,
    limit: int,
) -> SearchResult:
    """对 audit 域 ``turn_summaries`` 表的 ``summary`` 列做关键词 OR 匹配。

    策略:PG 端 ``ILIKE`` 利用 ``idx_ts_summary_trgm`` (GIN trigram) 走索引,
    单条 ``TurnSummary`` 行直接产出 ``MatchItem``,不再像旧版从 JSONB 数组
    遍历展开。短源整段返回。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID。
        keywords: 关键词列表。
        start: 带时区的起始日期（含）。
        end: 带时区的结束日期（不含）。
        limit: 返回结果上限。

    Returns:
        搜索结果 ``SearchResult`` (has_more + match_list)。
    """
    if not keywords:
        return SearchResult(has_more=False, match_list=[])

    from app.domain.audit.models import TurnSummary

    stmt = (
        select(TurnSummary)
        .join(Session, Session.id == TurnSummary.session_id)
        .where(
            Session.child_user_id == child_user_id,
            Session.created_at >= start,
            Session.created_at < end,
            or_(
                *[
                    TurnSummary.summary.ilike(f"%{_escape_like(kw)}%", escape="\\")
                    for kw in keywords
                ]
            ),
        )
        .distinct()
        .order_by(TurnSummary.created_at.desc())
        .limit(limit + 1)
    )
    scalars = (await db.execute(stmt)).scalars()
    match_list = [
        MatchItem(
            ref=scalar.id,
            source=SearchSourceType.TURN_SUMMARY,
            snippet=scalar.summary,
            occurred_at=scalar.created_at,
            locating=f"session: {scalar.session_id} turn: {scalar.turn_number}",
        )
        for scalar in scalars
    ]
    return SearchResult(has_more=len(match_list) > limit, match_list=match_list[:limit])


async def search_session_notes(
    db: AsyncSession,
    child_user_id: UUID,
    keywords: list[str],
    start: datetime,
    end: datetime,
    limit: int,
    context_chars: int,
) -> SearchResult:
    """对 ``rolling_summaries.session_notes`` 做关键词 OR 匹配,每个 session 一条 MatchItem。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID。
        keywords: 关键词列表。
        start: 带时区的起始日期（含）。
        end: 带时区的结束日期（不含）。
        limit: 返回结果上限。
        context_chars: 上下文窗口字符数（_extract_snippet 使用）。

    Returns:
        搜索结果 ``SearchResult``。
    """
    if not keywords:
        return SearchResult(has_more=False, match_list=[])

    from app.domain.audit.models import RollingSummary

    stmt = (
        select(RollingSummary)
        .join(Session, Session.id == RollingSummary.session_id)
        .where(
            Session.child_user_id == child_user_id,
            Session.created_at >= start,
            Session.created_at < end,
            or_(
                *[
                    RollingSummary.session_notes.ilike(f"%{_escape_like(kw)}%", escape="\\")
                    for kw in keywords
                ]
            ),
        )
        .distinct()
        .order_by(RollingSummary.created_at.desc())
        .limit(limit + 1)
    )
    scalars = (await db.execute(stmt)).scalars()
    match_list = [
        MatchItem(
            ref=scalar.id,
            source=SearchSourceType.SESSION_NOTES,
            snippet=_extract_snippet(scalar.session_notes or "", keywords, context_chars),
            occurred_at=scalar.updated_at,
            locating=f"session: {scalar.session_id}",
        )
        for scalar in scalars
    ]
    return SearchResult(has_more=len(match_list) > limit, match_list=match_list[:limit])


async def search_crisis_topics(
    db: AsyncSession,
    child_user_id: UUID,
    keywords: list[str],
    start: datetime,
    end: datetime,
    limit: int,
) -> SearchResult:
    """对 ``audit_records.crisis_topic`` 做关键词 OR 匹配,每条 crisis 记录一条 MatchItem。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID。
        keywords: 关键词列表。
        start: 带时区的起始日期（含）。
        end: 带时区的结束日期（不含）。
        limit: 返回结果上限。

    Returns:
        搜索结果 ``SearchResult``。
    """
    if not keywords:
        return SearchResult(has_more=False, match_list=[])

    stmt = (
        select(AuditRecord)
        .join(Session, Session.id == AuditRecord.session_id)
        .where(
            Session.child_user_id == child_user_id,
            Session.created_at >= start,
            Session.created_at < end,
            AuditRecord.crisis_topic.isnot(None),
            or_(
                *[
                    AuditRecord.crisis_topic.ilike(f"%{_escape_like(kw)}%", escape="\\")
                    for kw in keywords
                ]
            ),
        )
        .distinct()
        .order_by(AuditRecord.created_at.desc())
        .limit(limit + 1)
    )
    scalars = (await db.execute(stmt)).scalars()
    match_list = [
        MatchItem(
            ref=scalar.id,
            source=SearchSourceType.CRISIS_TOPIC,
            snippet=scalar.crisis_topic or "",
            occurred_at=scalar.created_at,
            locating=f"session: {scalar.session_id} turn: {scalar.turn_number}",
        )
        for scalar in scalars
    ]
    return SearchResult(has_more=len(match_list) > limit, match_list=match_list[:limit])


# ---------------------------------------------------------------------------
# DailyReport 6 列搜索:每行展开为 1..6 条 result(每列命中各一份)
# ---------------------------------------------------------------------------

SIX_SECTIONS = (
    DailyReport.today_overview,
    DailyReport.what_was_discussed,
    DailyReport.emotion_changes,
    DailyReport.noteworthy,
    DailyReport.suggestions,
    DailyReport.anomaly_periods,
)
"""daily_reports 的 6 段文本列名(与模型同序)。"""


async def search_daily_reports(
    db: AsyncSession,
    child_user_id: UUID,
    keywords: list[str],
    start_date: datetime,
    end_date: datetime,
    limit: int,
    context_chars: int,
) -> SearchResult:
    """跨 daily_reports 6 段文本列做 OR 匹配,每条命中的 report 产生 1 条 MatchItem。

    行为变更(M11):旧版本按"每行 × 每命中列"展开为 1..6 条 result;
    现版本合并 6 段为单一 full_text 后取一次 snippet,既保留跨段语境,
    又把 LLM 可见结果数控制为"每日报 ≤ 1 条",避免重复刷屏。

    当日 report 的排斥由调用方通过 ``start_date`` / ``end_date`` 窗口收紧来保证
    (end_date 默认 = report_date - 1 日)。本函数不再单独接受 ``exclude_report_date``
    参数(若产品后续要排除,沿用窗口过滤即可,无需新增签名)。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID。
        keywords: 关键词列表。
        start_date: 带时区的起始日期（含）。
        end_date: 带时区的结束日期（不含）。
        limit: 返回结果行级上限。
        context_chars: 上下文窗口字符数。

    Returns:
        搜索结果 ``SearchResult``,每条命中的 daily_report 1 条 MatchItem。
    """
    if not keywords:
        return SearchResult(has_more=False, match_list=[])

    stmt = (
        select(DailyReport)
        .where(
            DailyReport.child_user_id == child_user_id,
            DailyReport.report_date >= start_date,
            DailyReport.report_date < end_date,
            or_(
                *[
                    col.ilike(f"%{_escape_like(kw)}%", escape="\\")
                    for kw in keywords
                    for col in SIX_SECTIONS
                ],
            ),
        )
        .distinct()
        .order_by(DailyReport.report_date.desc())
        .limit(limit + 1)
    )
    scalars = (await db.execute(stmt)).scalars()
    match_list: list[MatchItem] = []
    for scalar in scalars:
        full_text = f"""\
# 今日概览
{scalar.today_overview}

# 聊了什么
{scalar.what_was_discussed}

# 情绪变化
{scalar.emotion_changes}

# 值得关注
{scalar.noteworthy}

# 具体建议
{scalar.suggestions}

# 异常时段标注
{scalar.anomaly_periods}"""
        match_list.append(
            MatchItem(
                ref=scalar.id,
                source=SearchSourceType.DAILY_REPORT,
                snippet=_extract_snippet(full_text, keywords, context_chars),
                occurred_at=scalar.updated_at,
                locating=f"session: {scalar.session_id}",
            )
        )
    return SearchResult(has_more=len(match_list) > limit, match_list=match_list[:limit])


# ---------------------------------------------------------------------------
# 按 ref 取完整原文
# ---------------------------------------------------------------------------


async def fetch_turn_messages(
    db: AsyncSession,
    child_user_id: UUID,
    source: Literal[SearchSourceType.TURN_SUMMARY, SearchSourceType.CRISIS_TOPIC],
    ref: UUID,
    context_turns: int = 0,
) -> str | None:
    """按 ``(source, ref)`` 取目标轮所在上下文窗口内的 human/ai 消息原文。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID。messages 查询 join Session 二次过滤,
            防止 ref 指向被另一 child 共享的(理论上不可能但防御)session。
        source: ``SearchSourceType.TURN_SUMMARY`` 或 ``SearchSourceType.CRISIS_TOPIC``;
            二者均产出 ``(session_id, turn_number)`` 锚点。
        ref: 该锚点行主键 UUID。
        context_turns: 上下展轮数 (0-3)。

    Returns:
        ``list[FetchMessageResult]`` 的 JSON 字符串(便于直接喂回 LLM tool result)。
        ref 找不到对应行 → None;跨 child 取值时,messages JOIN 把所有消息滤空,
        返回 ``"[]"``(不返回 None,但内容空,不构成信息泄漏)。
    """

    if source == SearchSourceType.TURN_SUMMARY:
        scalar = await db.get(TurnSummary, ref)
    elif source == SearchSourceType.CRISIS_TOPIC:
        scalar = await db.get(AuditRecord, ref)

    if scalar is None:
        return None

    session_id = scalar.session_id
    turn_number = scalar.turn_number
    min_turn = turn_number - context_turns
    max_turn = turn_number + context_turns

    messages = (
        (
            await db.execute(
                select(Message)
                .join(Session, Session.id == Message.session_id)
                .where(
                    Message.session_id == session_id,
                    Session.child_user_id == child_user_id,
                    Message.turn_number.between(min_turn, max_turn),
                    Message.status != MessageStatus.discarded,
                    Message.role.in_([MessageRole.ai, MessageRole.human]),
                )
                # desc + id desc:相同 created_at 时按 id 倒序固定次序,避免
                # 同轮 H/A 的两个不同顺序出现,LLC 视角混乱。
                .order_by(Message.created_at.desc(), Message.id.desc())
            )
        )
        .scalars()
        .all()
    )
    adapter = TypeAdapter(list[FetchMessageResult])
    fetch_result = adapter.dump_json(adapter.validate_python(messages)).decode()

    return fetch_result


async def fetch_notes(
    db: AsyncSession,
    child_user_id: UUID,
    rolling_summary_id: UUID,
) -> str | None:
    """按 ``RollingSummary.id`` 取整段 ``session_notes`` + 元信息。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID。query 中 join Session 做所有权校验,
            跨 child 取值会被滤掉(handler 层无需再判)。
        rolling_summary_id: ``RollingSummary`` 主键 UUID(由 SearchHistory 检索返回)。

    Returns:
        ``FetchRollingSummaryResult`` 的 JSON 字符串;若不存在或跨 child 则 None。
    """
    scalar = await db.scalar(
        select(RollingSummary)
        .join(Session, Session.id == RollingSummary.session_id)
        .where(RollingSummary.id == rolling_summary_id, Session.child_user_id == child_user_id)
    )
    if scalar is None:
        return None

    note = FetchRollingSummaryResult.model_validate(scalar).model_dump_json()

    return note


async def fetch_report(
    db: AsyncSession,
    child_user_id: UUID,
    report_id: UUID,
) -> str | None:
    """按 ``DailyReport.id`` 取整条结构化报告。

    历史版本 ``generic,不加 child_user_id 过滤`` 由 tools 层手工校验;
    现版本内联 join Session 做所有权校验,跨 child 取值被 SQL 滤掉。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID。
        report_id: daily_report 的 UUID。

    Returns:
        ``FetchDailyReportResult`` 的 JSON 字符串;不存在或跨 child 则 None。
    """

    scalar = await db.scalar(
        select(DailyReport)
        .join(Session, Session.id == DailyReport.session_id)
        .where(DailyReport.id == report_id, Session.child_user_id == child_user_id)
    )
    if scalar is None:
        return None

    fetch_result = FetchDailyReportResult.model_validate(scalar).model_dump_json()
    return fetch_result
