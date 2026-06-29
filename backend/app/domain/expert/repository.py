"""expert 域数据源仓储层：ORM 只读查询。

7 个只读函数 + 2 个 helper，全部走 ORM（``select`` + ``.where()``）。
仅在 PG 专有语法必需时（本文件当前无）用 ``text()``。

表依赖（只读，不跨域 import ORM 模型）：
- sessions (chat 域)
- messages (chat 域)
- rolling_summaries (audit 域)
- audit_records (audit 域)
- daily_reports (expert 域)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import any_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit.schemas import TurnSummaryEntry

# ---------------------------------------------------------------------------
# 返回格式
# ---------------------------------------------------------------------------

SEARCH_RESULT_KEYS = {"ref", "source", "snippet", "occurred_at", "matched", "locating"}
"""每次命中返回 dict 的标准 key 集合。"""


def _make_result(
    *,
    ref: str,
    source: str,
    snippet: str,
    occurred_at: str | None,
    matched: list[str],
    locating: str,
) -> dict[str, Any]:
    """组装一条标准搜索结果 dict。"""
    return {
        "ref": ref,
        "source": source,
        "snippet": snippet,
        "occurred_at": occurred_at,
        "matched": matched,
        "locating": locating,
    }


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


def _match_matched(
    text: str,
    keywords: list[str],
) -> list[str]:
    """返回在 text 中命中的关键词列表（去重，按首次出现顺序）。"""
    text_lower = text.lower()
    seen: set[str] = set()
    matched: list[str] = []
    for kw in keywords:
        if kw.lower() in text_lower and kw.lower() not in seen:
            seen.add(kw.lower())
            matched.append(kw)
    return matched


# ---------------------------------------------------------------------------
# 主查询函数
# ---------------------------------------------------------------------------


async def search_turn_summaries(
    db: AsyncSession,
    child_user_id: str,
    keywords: list[str],
    start: datetime,
    end: datetime,
    limit: int,
    context_chars: int,
) -> list[dict[str, Any]]:
    """对 ``rolling_summaries.turn_summaries`` JSONB 做关键词 OR 匹配。

    策略:ORM 拉取该 child + 日期窗口内所有含 turn_summaries 的行,
    Python 端迭代 turn_summaries 数组、字符串匹配关键词。短源整段返回。
    不跨域 Python import 触发,在函数体内 inline import RollingSummary / Session。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID 字符串。
        keywords: 关键词列表。
        start: 起始日期（含）。
        end: 结束日期（含）。
        limit: 返回结果上限。
        context_chars: 上下文窗口字符数（短源忽略）。

    Returns:
        搜索结果列表。
    """
    if not keywords:
        return []

    from app.domain.audit.models import RollingSummary
    from app.domain.chat.models import Session

    stmt = (
        select(
            RollingSummary.session_id,
            RollingSummary.turn_summaries,
            Session.created_at,
        )
        .join(Session, Session.id == RollingSummary.session_id)
        .where(
            Session.child_user_id == child_user_id,
            RollingSummary.turn_summaries.isnot(None),
            Session.created_at >= start,
            Session.created_at < end,
        )
    )
    rows = (await db.execute(stmt)).all()

    results: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row.session_id)
        summaries: list[TurnSummaryEntry] = row.turn_summaries or []
        for entry in summaries:
            summary = entry.summary
            if not summary:
                continue
            if not _match_matched(summary, keywords):
                continue
            turn_num = entry.turn_number
            ref = f"turn:{sid}#{turn_num}"
            results.append(
                _make_result(
                    ref=ref,
                    source="turn_summary",
                    snippet=summary,
                    occurred_at=None,  # turn_summaries 无精确时间戳
                    matched=_match_matched(summary, keywords),
                    locating=f"session {sid} 第 {turn_num} 轮",
                )
            )
            if len(results) >= limit:
                return results
    return results


async def search_session_notes(
    db: AsyncSession,
    child_user_id: str,
    keywords: list[str],
    start: date | None,
    end: date | None,
    limit: int,
    context_chars: int,
) -> list[dict[str, Any]]:
    """对 ``rolling_summaries.session_notes`` 做关键词 OR 匹配。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID 字符串。
        keywords: 关键词列表。
        start: 起始日期（含）。
        end: 结束日期（含）。
        limit: 返回结果上限。
        context_chars: 上下文窗口字符数（_extract_snippet 使用）。

    Returns:
        搜索结果列表。
    """
    if not keywords:
        return []

    from app.domain.audit.models import RollingSummary
    from app.domain.chat.models import Session

    stmt = (
        select(
            RollingSummary.session_id,
            RollingSummary.session_notes,
            RollingSummary.updated_at,
            Session.created_at,
        )
        .join(Session, Session.id == RollingSummary.session_id)
        .where(
            Session.child_user_id == child_user_id,
            RollingSummary.session_notes.isnot(None),
            RollingSummary.session_notes != "",
            or_(start is None, Session.created_at >= start),  # type: ignore[arg-type]
            or_(end is None, Session.created_at <= end),  # type: ignore[arg-type]
        )
        .order_by(Session.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()

    results: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row.session_id)
        notes: str = row.session_notes or ""
        if not _match_matched(notes, keywords):
            continue
        snippet = _extract_snippet(notes, keywords, context_chars)
        matched = _match_matched(notes, keywords)
        time_range = await _get_session_time_range(db, sid)
        locating_parts = [f"session {sid}"]
        if time_range:
            locating_parts.append(time_range)
        results.append(
            _make_result(
                ref=f"notes:{sid}",
                source="session_notes",
                snippet=snippet,
                occurred_at=str(row.updated_at) if row.updated_at else None,
                matched=matched,
                locating="; ".join(locating_parts),
            )
        )
        if len(results) >= limit:
            return results
    return results


async def search_crisis_topics(
    db: AsyncSession,
    child_user_id: str,
    keywords: list[str],
    start: date | None,
    end: date | None,
    limit: int,
) -> list[dict[str, Any]]:
    """对 ``audit_records.crisis_topic`` 做关键词 OR 匹配。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID 字符串。
        keywords: 关键词列表。
        start: 起始日期（含）。
        end: 结束日期（含）。
        limit: 返回结果上限。

    Returns:
        搜索结果列表。
    """
    if not keywords:
        return []

    from app.domain.audit.models import AuditRecord
    from app.domain.chat.models import Session

    stmt = (
        select(
            AuditRecord.session_id,
            AuditRecord.turn_number,
            AuditRecord.crisis_topic,
            AuditRecord.created_at,
        )
        .join(Session, Session.id == AuditRecord.session_id)
        .where(
            Session.child_user_id == child_user_id,
            AuditRecord.crisis_detected.is_(True),
            AuditRecord.crisis_topic.isnot(None),
            or_(start is None, AuditRecord.created_at >= start),  # type: ignore[arg-type]
            or_(end is None, AuditRecord.created_at <= end),  # type: ignore[arg-type]
        )
        .order_by(AuditRecord.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()

    results: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row.session_id)
        topic: str = row.crisis_topic or ""
        if not _match_matched(topic, keywords):
            continue
        matched = _match_matched(topic, keywords)
        results.append(
            _make_result(
                ref=f"turn:{sid}#{row.turn_number}",
                source="crisis_topic",
                snippet=topic,
                occurred_at=str(row.created_at) if row.created_at else None,
                matched=matched,
                locating=f"session {sid} 第 {row.turn_number} 轮（危机）",
            )
        )
        if len(results) >= limit:
            return results
    return results


# ---------------------------------------------------------------------------
# DailyReport 6 列搜索:每行展开为 1..6 条 result(每列命中各一份)
# ---------------------------------------------------------------------------

SIX_SECTIONS: tuple[str, ...] = (
    "today_overview",
    "what_was_discussed",
    "emotion_changes",
    "noteworthy",
    "suggestions",
    "anomaly_periods",
)
"""daily_reports 的 6 段文本列名(与模型同序)。"""


async def search_daily_reports(
    db: AsyncSession,
    child_user_id: str,
    keywords: list[str],
    start_date: date | None,
    end_date: date | None,
    limit: int,
    context_chars: int,
    exclude_report_date: date | None = None,
) -> list[dict[str, Any]]:
    """跨 daily_reports 6 段文本列做 OR 匹配,每行展开为 1..6 条 result。

    每行最多 6 条 result(对应 6 个命中列各出一份);实际返回总数可能 > limit。
    ``limit`` 为行级上限(SQL ``LIMIT N``),展开后 ``total`` 可能 > ``limit``。
    ``locating`` 字段标记命中的段名,LLM 可见结构。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID 字符串。
        keywords: 关键词列表。
        start_date: 起始日期（含）。
        end_date: 结束日期（含）。
        limit: 返回结果行级上限。
        context_chars: 上下文窗口字符数。
        exclude_report_date: 排除的报告日期（通常是当天正在生成的报告日期）。

    Returns:
        搜索结果列表,每行 1..6 条。
    """
    if not keywords:
        return []

    from app.domain.expert.models import DailyReport

    kw_patterns: list[str] = [f"%{_escape_like(kw)}%" for kw in keywords]
    kw_array_expr: Any = func.array(kw_patterns)  # ARRAY[TEXT] 推导
    sect_attrs = [getattr(DailyReport, c) for c in SIX_SECTIONS]

    stmt = (
        select(
            DailyReport.id,
            DailyReport.report_date,
            DailyReport.created_at,
            DailyReport.today_overview,
            DailyReport.what_was_discussed,
            DailyReport.emotion_changes,
            DailyReport.noteworthy,
            DailyReport.suggestions,
            DailyReport.anomaly_periods,
        )
        .where(
            DailyReport.child_user_id == child_user_id,
            or_(start_date is None, DailyReport.report_date >= start_date),  # type: ignore[arg-type]
            or_(end_date is None, DailyReport.report_date <= end_date),  # type: ignore[arg-type]
            or_(
                exclude_report_date is None,  # type: ignore[arg-type]
                DailyReport.report_date != exclude_report_date,
            ),
            or_(
                *[attr.ilike(any_(kw_array_expr), escape="\\") for attr in sect_attrs],
            ),
        )
        .order_by(DailyReport.report_date.desc())
        .limit(limit)  # 行级 limit,每行最多 6 条 result
    )
    rows = (await db.execute(stmt)).all()

    results: list[dict[str, Any]] = []
    for row in rows:
        rid = str(row.id)
        report_date_str = str(row.report_date)
        for col in SIX_SECTIONS:
            val: str = getattr(row, col) or ""
            if not _match_matched(val, keywords):
                continue
            snippet = _extract_snippet(val, keywords, context_chars)
            results.append(
                _make_result(
                    ref=f"report:{rid}",
                    source="daily_report",
                    snippet=snippet,
                    occurred_at=str(row.created_at) if row.created_at else None,
                    matched=_match_matched(val, keywords),
                    locating=f"日报 {report_date_str} {col} 段",
                )
            )
    return results


# ---------------------------------------------------------------------------
# 按 ref 取完整原文
# ---------------------------------------------------------------------------


async def fetch_turn(
    db: AsyncSession,
    session_id: str,
    turn_number: int,
    context_turns: int = 0,
) -> dict[str, Any] | None:
    """返回 turn_summary + human/ai 消息 + crisis 标记。

    以目标 turn 为中心，展开前后 context_turns 轮的消息原文。

    Args:
        db: 异步 DB session。
        session_id: session UUID 字符串。
        turn_number: 目标轮次编号。
        context_turns: 展开前后各 N 轮（0-3）。

    Returns:
        包含 turn_summary、messages、crisis 标记的 dict；若不存在则返回 None。
    """
    from app.domain.audit.models import AuditRecord, RollingSummary
    from app.domain.chat.models import Message

    # 1. 获取 turn_summary
    ts_row = (
        (await db.execute(select(RollingSummary).where(RollingSummary.session_id == session_id)))
        .scalars()
        .first()
    )
    if ts_row is None:
        return None

    turn_summary: str | None = None
    summaries = ts_row.turn_summaries
    if summaries:
        for entry in summaries:
            if entry.turn_number == turn_number:
                turn_summary = entry.summary
                break

    # 2. 获取 crisis 标记
    crisis_row = (
        (
            await db.execute(
                select(AuditRecord).where(
                    AuditRecord.session_id == session_id,
                    AuditRecord.turn_number == turn_number,
                )
            )
        )
        .scalars()
        .first()
    )

    crisis_detected = crisis_row.crisis_detected if crisis_row else False
    crisis_topic = crisis_row.crisis_topic if crisis_row else None

    # 3. 获取目标轮附近的消息原文
    min_turn = max(1, turn_number - context_turns)
    max_turn = turn_number + context_turns

    msg_rows = (
        (
            await db.execute(
                select(Message)
                .where(
                    Message.session_id == session_id,
                    Message.turn_number.between(min_turn, max_turn),
                    Message.status == "active",
                )
                .order_by(Message.turn_number, Message.created_at)
            )
        )
        .scalars()
        .all()
    )

    messages: list[dict[str, Any]] = []
    for m in msg_rows:
        messages.append(
            {
                "role": m.role.value if hasattr(m.role, "value") else m.role,
                "content": m.content,
                "turn_number": m.turn_number,
                "created_at": str(m.created_at) if m.created_at else None,
            }
        )

    # 4. 获取 session 时间范围
    time_range = await _get_session_time_range(db, session_id)

    return {
        "session_id": session_id,
        "turn_number": turn_number,
        "turn_summary": turn_summary,
        "crisis_detected": crisis_detected,
        "crisis_topic": crisis_topic,
        "session_time_range": time_range,
        "messages": messages,
    }


async def fetch_notes(
    db: AsyncSession,
    session_id: str,
) -> dict[str, Any] | None:
    """返回 rolling_summaries.session_notes 全文 + 元信息。

    Args:
        db: 异步 DB session。
        session_id: session UUID 字符串。

    Returns:
        包含 session_notes、last_turn、session_created_at 的 dict；
        若不存在则返回 None。
    """
    from app.domain.audit.models import RollingSummary
    from app.domain.chat.models import Session

    stmt = (
        select(RollingSummary, Session.created_at)
        .join(Session, RollingSummary.session_id == Session.id)
        .where(RollingSummary.session_id == session_id)
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        return None

    rs_row, session_created_at = row
    return {
        "session_id": session_id,
        "session_notes": rs_row.session_notes,
        "last_turn": rs_row.last_turn,
        "updated_at": str(rs_row.updated_at) if rs_row.updated_at else None,
        "session_created_at": str(session_created_at) if session_created_at else None,
    }


async def fetch_report(
    db: AsyncSession,
    report_id: str,
) -> dict[str, Any] | None:
    """返回完整 daily_report 结构化 dict。generic,不加 child_user_id 过滤。

    Args:
        db: 异步 DB session。
        report_id: daily_report 的 UUID 字符串。

    Returns:
        包含 report 全部字段的结构化 dict；不存在则返回 None。
    """
    from app.domain.expert.models import DailyReport

    stmt = select(DailyReport).where(DailyReport.id == report_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None

    return {
        "id": str(row.id),
        "child_user_id": str(row.child_user_id),
        "session_id": str(row.session_id),
        "report_date": row.report_date.isoformat(),
        "overall_status": row.overall_status.value,
        "degraded": row.degraded,
        "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
        "today_overview": row.today_overview,
        "what_was_discussed": row.what_was_discussed,
        "emotion_changes": row.emotion_changes,
        "noteworthy": row.noteworthy,
        "suggestions": row.suggestions,
        "anomaly_periods": row.anomaly_periods,
    }
