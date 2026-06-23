"""expert 域数据源仓储层：原始 SQL 只读查询。

7 个只读函数 + 2 个 helper，全部通过 `from sqlalchemy import text` 执行原始 SQL。

表依赖（只读，不跨域 import ORM 模型）：
- sessions (chat 域)
- messages (chat 域)
- rolling_summaries (audit 域)
- audit_records (audit 域)
- daily_reports (expert 域)
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    stmt = text("""
        SELECT
            to_char(min(m.created_at), 'YYYY-MM-DD"T"HH24:MI:SS'),
            to_char(max(m.created_at), 'YYYY-MM-DD"T"HH24:MI:SS')
        FROM messages m
        WHERE m.session_id = :session_id
    """)
    row = (await db.execute(stmt, {"session_id": session_id})).one_or_none()
    if row is None or row[0] is None:
        return None
    return f"{row[0]} ~ {row[1]}"


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
    start: date | None,
    end: date | None,
    limit: int,
    context_chars: int,
) -> list[dict[str, Any]]:
    """对 rolling_summaries.turn_summaries JSONB 使用 jsonb_array_elements +
    ILIKE ANY，JOIN sessions 过滤 child_user_id + 时间窗口。短源整段返回。

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

    kw_patterns = [f"%{kw}%" for kw in keywords]

    stmt = text("""
        WITH ts AS (
            SELECT
                s.id AS session_id,
                (jsonb_array_elements(rs.turn_summaries) ->> 'turn')::int AS turn_num,
                jsonb_array_elements(rs.turn_summaries) ->> 'summary' AS summary,
                s.created_at AS session_created_at
            FROM rolling_summaries rs
            JOIN sessions s ON s.id = rs.session_id
            WHERE s.child_user_id = :child_user_id
              AND rs.turn_summaries IS NOT NULL
              AND (:start::date IS NULL OR s.created_at::date >= :start)
              AND (:end::date IS NULL OR s.created_at::date <= :end)
        )
        SELECT
            session_id,
            turn_num,
            summary
        FROM ts
        WHERE (:kw_filter = '1=1' OR summary ILIKE ANY(:keywords))
        ORDER BY session_id, turn_num
        LIMIT :limit
    """)

    kw_filter = "1=0"
    if kw_patterns:
        kw_filter = "1=1"

    params = {
        "child_user_id": child_user_id,
        "start": start,
        "end": end,
        "limit": limit,
        "keywords": kw_patterns,
        "kw_filter": kw_filter,
    }

    rows = (await db.execute(stmt, params)).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row.session_id)
        summary: str = row.summary or ""
        ref = f"turn:{sid}#{row.turn_num}"
        matched = _match_matched(summary, keywords)
        results.append(
            _make_result(
                ref=ref,
                source="turn_summary",
                snippet=summary,
                occurred_at=None,  # turn_summaries 无精确时间戳
                matched=matched,
                locating=f"session {sid} 第 {row.turn_num} 轮",
            )
        )
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
    """对 rolling_summaries.session_notes TEXT 用 ILIKE ANY。

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

    kw_patterns = [f"%{kw}%" for kw in keywords]

    stmt = text("""
        SELECT
            s.id AS session_id,
            rs.session_notes,
            s.created_at AS session_created_at,
            rs.updated_at
        FROM rolling_summaries rs
        JOIN sessions s ON s.id = rs.session_id
        WHERE s.child_user_id = :child_user_id
          AND rs.session_notes IS NOT NULL
          AND rs.session_notes != ''
          AND (:start::date IS NULL OR s.created_at::date >= :start)
          AND (:end::date IS NULL OR s.created_at::date <= :end)
          AND (:kw_filter = '1=1' OR rs.session_notes ILIKE ANY(:keywords))
        ORDER BY s.created_at DESC
        LIMIT :limit
    """)

    kw_filter = "1=0"
    if kw_patterns:
        kw_filter = "1=1"

    params = {
        "child_user_id": child_user_id,
        "start": start,
        "end": end,
        "limit": limit,
        "keywords": kw_patterns,
        "kw_filter": kw_filter,
    }

    rows = (await db.execute(stmt, params)).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row.session_id)
        notes: str = row.session_notes or ""
        snippet = _extract_snippet(notes, keywords, context_chars)
        matched = _match_matched(notes, keywords)
        # 获取时间范围作为 locating 信息
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
    return results


async def search_crisis_topics(
    db: AsyncSession,
    child_user_id: str,
    keywords: list[str],
    start: date | None,
    end: date | None,
    limit: int,
) -> list[dict[str, Any]]:
    """对 audit_records.crisis_topic + JOIN sessions。

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

    kw_patterns = [f"%{kw}%" for kw in keywords]

    stmt = text("""
        SELECT
            s.id AS session_id,
            ar.turn_number,
            ar.crisis_topic,
            ar.created_at
        FROM audit_records ar
        JOIN sessions s ON s.id = ar.session_id
        WHERE s.child_user_id = :child_user_id
          AND ar.crisis_detected = TRUE
          AND ar.crisis_topic IS NOT NULL
          AND (:start::date IS NULL OR ar.created_at::date >= :start)
          AND (:end::date IS NULL OR ar.created_at::date <= :end)
          AND (:kw_filter = '1=1' OR ar.crisis_topic ILIKE ANY(:keywords))
        ORDER BY ar.created_at DESC
        LIMIT :limit
    """)

    kw_filter = "1=0"
    if kw_patterns:
        kw_filter = "1=1"

    params = {
        "child_user_id": child_user_id,
        "start": start,
        "end": end,
        "limit": limit,
        "keywords": kw_patterns,
        "kw_filter": kw_filter,
    }

    rows = (await db.execute(stmt, params)).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row.session_id)
        topic: str = row.crisis_topic or ""
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
    return results


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
    """对 daily_reports.content，排除 exclude_report_date。

    Args:
        db: 异步 DB session。
        child_user_id: 孩子用户 UUID 字符串。
        keywords: 关键词列表。
        start_date: 起始日期（含）。
        end_date: 结束日期（含）。
        limit: 返回结果上限。
        context_chars: 上下文窗口字符数（_extract_snippet 使用）。
        exclude_report_date: 排除的报告日期（通常是当天正在生成的报告日期）。

    Returns:
        搜索结果列表。
    """
    if not keywords:
        return []

    kw_patterns = [f"%{kw}%" for kw in keywords]

    stmt = text("""
        SELECT
            dr.id,
            dr.report_date,
            dr.content,
            dr.created_at
        FROM daily_reports dr
        WHERE dr.child_user_id = :child_user_id
          AND (:start_date::date IS NULL OR dr.report_date >= :start_date)
          AND (:end_date::date IS NULL OR dr.report_date <= :end_date)
          AND (:exclude_date::date IS NULL OR dr.report_date != :exclude_date)
          AND (:kw_filter = '1=1' OR dr.content ILIKE ANY(:keywords))
        ORDER BY dr.report_date DESC
        LIMIT :limit
    """)

    kw_filter = "1=0"
    if kw_patterns:
        kw_filter = "1=1"

    params = {
        "child_user_id": child_user_id,
        "start_date": start_date,
        "end_date": end_date,
        "exclude_date": exclude_report_date,
        "limit": limit,
        "keywords": kw_patterns,
        "kw_filter": kw_filter,
    }

    rows = (await db.execute(stmt, params)).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        rid = str(row.id)
        content: str = row.content
        snippet = _extract_snippet(content, keywords, context_chars)
        matched = _match_matched(content, keywords)
        results.append(
            _make_result(
                ref=f"report:{rid}",
                source="daily_report",
                snippet=snippet,
                occurred_at=str(row.created_at) if row.created_at else None,
                matched=matched,
                locating=f"日报 {row.report_date} (id: {rid})",
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
    # 1. 获取 turn_summary
    ts_stmt = text("""
        SELECT
            rs.turn_summaries,
            rs.session_notes
        FROM rolling_summaries rs
        WHERE rs.session_id = :session_id
    """)
    ts_row = (await db.execute(ts_stmt, {"session_id": session_id})).one_or_none()
    if ts_row is None:
        return None

    turn_summary: str | None = None
    summaries = ts_row.turn_summaries
    if summaries:
        for entry in summaries:
            if isinstance(entry, dict) and entry.get("turn") == turn_number:
                turn_summary = entry.get("summary")
                break

    # 2. 获取 crisis 标记
    crisis_stmt = text("""
        SELECT
            ar.crisis_detected,
            ar.crisis_topic
        FROM audit_records ar
        WHERE ar.session_id = :session_id
          AND ar.turn_number = :turn_number
        LIMIT 1
    """)
    crisis_row = (
        await db.execute(
            crisis_stmt,
            {
                "session_id": session_id,
                "turn_number": turn_number,
            },
        )
    ).one_or_none()

    crisis_detected = crisis_row.crisis_detected if crisis_row else False
    crisis_topic = crisis_row.crisis_topic if crisis_row else None

    # 3. 获取目标轮附近的消息原文
    min_turn = max(1, turn_number - context_turns)
    max_turn = turn_number + context_turns

    msg_stmt = text("""
        SELECT
            m.role,
            m.content,
            m.turn_number,
            m.created_at
        FROM messages m
        WHERE m.session_id = :session_id
          AND m.turn_number BETWEEN :min_turn AND :max_turn
          AND m.status = 'active'
        ORDER BY m.turn_number, m.created_at
    """)
    msg_rows = (
        await db.execute(
            msg_stmt,
            {
                "session_id": session_id,
                "min_turn": min_turn,
                "max_turn": max_turn,
            },
        )
    ).fetchall()

    messages: list[dict[str, Any]] = []
    for m in msg_rows:
        messages.append(
            {
                "role": m.role,
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
    stmt = text("""
        SELECT
            rs.session_notes,
            rs.last_turn,
            rs.updated_at,
            s.created_at AS session_created_at
        FROM rolling_summaries rs
        JOIN sessions s ON s.id = rs.session_id
        WHERE rs.session_id = :session_id
    """)
    row = (await db.execute(stmt, {"session_id": session_id})).one_or_none()
    if row is None:
        return None

    return {
        "session_id": session_id,
        "session_notes": row.session_notes,
        "last_turn": row.last_turn,
        "updated_at": str(row.updated_at) if row.updated_at else None,
        "session_created_at": str(row.session_created_at) if row.session_created_at else None,
    }


async def fetch_report(
    db: AsyncSession,
    report_id: str,
) -> dict[str, Any]:
    """返回完整 daily_report 内容。不存在则 raise ValueError。

    Args:
        db: 异步 DB session。
        report_id: daily_report 的 UUID 字符串。

    Returns:
        包含 report 全部字段的 dict。

    Raises:
        ValueError: 指定 report_id 不存在。
    """
    stmt = text("""
        SELECT
            dr.id,
            dr.child_user_id,
            dr.report_date,
            dr.overall_status,
            dr.dimension_summary,
            dr.content,
            dr.delivered_at,
            dr.created_at
        FROM daily_reports dr
        WHERE dr.id = :report_id
    """)
    row = (await db.execute(stmt, {"report_id": report_id})).one_or_none()
    if row is None:
        raise ValueError(f"Daily report not found: {report_id}")

    result: dict[str, Any] = {
        "id": str(row.id),
        "child_user_id": str(row.child_user_id),
        "report_date": str(row.report_date),
        "overall_status": row.overall_status,
        "dimension_summary": row.dimension_summary,
        "content": row.content,
        "delivered_at": str(row.delivered_at) if row.delivered_at else None,
        "created_at": str(row.created_at) if row.created_at else None,
    }
    return result
