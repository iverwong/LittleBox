# DailyReport 存储重构 + 检索工具加固 — 设计

## 背景

DailyReport 当前用单列 `content: Text` 存 markdown 拼接的 6 段报告正文,后续 `worker._parse_today_overview_from_content` 又要按字面量 `"## 今日概览\n\n"` 反向切出 `today_overview` 一节供次日 expert prompt 使用,形成"结构化 Pydantic → 拼 markdown → 存库 → parse 回来"的 round-trip 损耗 + 一道隐式字符串契约。

同步审查 expert 域代码,发现三处与本主题相关的真实问题:

- `_fetch_by_ref` 的 `report:{id}` 分支缺 ownership check,直接 `await fetch_report(db, rid)`,不验证 report 所属 child
- `SearchHistoryInput` 用 `sources: list[str]` 跨 4 源扇出,`limit` 语义模糊(per-source 拉 N 条再统一截 N),且不强制 LLM 单源检索
- 工具 handler 不接 `DBAPIError` 等 DB 异常,异常一路冒到 `graph.ainvoke`,被 worker 的 `return_exceptions=True` 兜住 → **该 child 当天日报直接 skip,不是 degraded**

此外,`repository.py` 整文件走 `text()` 裸 SQL(7 个只读函数),与项目"ORM 优先"原则不一致,CLAUDE.md 的 `ILIKE ANY` 例外条款是历史妥协,可借本 PR 收回。

## 目标

1. **消除 markdown parse 回路**:6 段文本直接落库,LLM 工具与次日 recency 上下文都直接读结构化列
2. **加 `session_id` 锚定**:为前端"日期 → session → report"拉取链路铺路,避免前端复算 boundary_hour 逻辑
3. **repository.py 全面 ORM 化**:撤回 `text()` 例外,CLAUDE.md 同步更新
4. **检索工具三个真问题修补**:A1 ownership check / A2 单源搜索 / A4 DB 异常 catch + 自然语言 error

## 非目标

- 不改 `ExpertReportSchema` Pydantic 字段(已经是 source of truth)
- 不改 LLM 的 `bind_tools` 契约(三个工具名 + 入参 schema 不变;只是入参的具体类型从 `sources: list[str]` 变 `source: str`,以及 `fetch_report` 返回的 dict 形态从 markdown 字符串变结构化)
- 不改 LLM prompt 模板(LLM 看到的 tool 返回从 markdown 字符串变成结构化 dict 属于 LLM 可感知变化,但 prompt 兼容)
- 不改 `expert/llm.py` / `expert/prompts.py`
- 不重构 `expert/graph.py` 的 220+ 行(后续 PR 处理)
- 不动 B 组(可读性 / 降级文案)

---

## 决策记录

| 决策 | 选项 | 选定 | 理由 |
|---|---|---|---|
| 6 段文本存储 | (a) 保留 `content` markdown / (b) 拆 6 列直写 / (c) JSONB blob | **(b)** | 结构化 Pydantic 已是 source of truth,markdown 拼接是冗余步骤 |
| `session_id` 列 | (a) 不加 / (b) 加 + FK + 索引 | **(b)** | chat.Session 无日期字段,前端"日期 → session"需复算 boundary_hour,加 session_id 锚定避免 |
| 跨域 FK 引用方式 | (a) 字符串引用 + `ondelete="CASCADE"` / (b) 不用 FK 跨域靠 UUID | **(a)** | 项目现有模式(`chat.Session`/`audit.AuditRecord`/`audit.RollingSummary` 全是 `ForeignKey("sessions.id", ondelete="CASCADE")`),CLAUDE.md"零 import"指 Python import 不指 DB FK 字符串 |
| `session_id` nullable | (a) `NOT NULL` / (b) nullable | **`NOT NULL`** | 产品逻辑:有聊天才有报告,没聊天不生成 |
| Upsert conflict target | (a) `(child, date)` 不变 / (b) `session_id` | **(a) `(child, date)`** | 产品不变量 1 child × 1 day = 1 report;upsert 走产品逻辑,不引入新约束 |
| 唯一索引 | (a) 只 `(child, date)` / (b) 只 `session_id` / (c) 两个都 unique | **(c) 两个都 unique** | Defense in depth,DB 层不依赖业务假设 |
| `SearchHistoryInput` 入参 | (a) `sources: list[str]` 跨源 / (b) `source: str` 单源 | **(b) 单源** | LLM 想要哪源就调哪源,`limit` 语义变干净;多源需求由 LLM 多次调用覆盖 |
| Tool handler DB 异常 catch | (a) 宽 catch `{DBAPIError, ResourceClosedError}` 但 `except ProgrammingError: raise` 在前 / (b) 窄 catch `{InterfaceError, OperationalError}` | **(a) 宽 catch + ProgrammingError 显式 re-raise** | 真实故障 `statement_timeout` 抛基类 `DBAPIError`(probe 锁死),窄 catch 会漏 → child skip;`ProgrammingError` 是代码 bug,通过其他路径(测试/code review/监控)守,不通过错误处理守 |
| ORM 化 | (a) 只改本次涉及的 `search_daily_reports` / (b) 全文件 ORM 化 + 删 CLAUDE.md 例外 | **(b) 全文件** | 项目原则归一,撤回历史妥协 |

---

## 模块 1:数据模型

### 1.1 `app/domain/expert/models.py` — DailyReport 改造

**改动:**

- **drop** `content: Text NOT NULL`
- **add 6 列** `Text NOT NULL`:
  - `today_overview`
  - `what_was_discussed`
  - `emotion_changes`
  - `noteworthy`
  - `suggestions`
  - `anomaly_periods`
- **add** `session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, comment="锚定当日 chat session,删除 child 时级联清理")`
- **更新** `__table_args__`:
  ```python
  __table_args__ = (
      Index("idx_reports_child", "child_user_id", "report_date", unique=True),
      Index("idx_reports_session", "session_id", unique=True),
  )
  ```

**保留不动:** `id` / `created_at` / `child_user_id` / `report_date` / `overall_status` / `dimension_summary` / `degraded` / `delivered_at`

### 1.2 索引设计

| 索引 | 覆盖查询 |
|---|---|
| `idx_reports_child (child_user_id, report_date) UNIQUE` | 前端按 (child, date) 查日报;worker recency 查 (`child=X AND report_date < Y ORDER BY report_date DESC LIMIT 5`);upsert conflict target |
| `idx_reports_session (session_id) UNIQUE` | 前端 session → report 反查;防御"同一 session 多次生成" |

**两个 unique 约束的语义不变量:**

- `(child_user_id, report_date) UNIQUE`:1 child × 1 day ≤ 1 report(产品不变量"日报每天一条")
- `(session_id) UNIQUE`:1 session ≤ 1 report(单 session 视角防御)

两个不变量在"1 child × 1 day = 1 session"前提下等价,但 DB 层不假设业务等价,各留一道闸。

### 1.3 跨域 FK 处理

`session_id` 用 `ForeignKey("sessions.id", ondelete="CASCADE")` 字符串引用。CLAUDE.md 说的"零跨域 import"指 **Python import 不指 DB FK 字符串**。现有 `audit.AuditRecord.session_id` / `audit.RollingSummary.session_id` / `chat.Session.child_user_id` 全是同款模式。

**级联链:**

```
DELETE FROM users WHERE id = :child
  ↓ ON DELETE CASCADE
DELETE FROM sessions WHERE child_user_id = :child
  ↓ ON DELETE CASCADE
DELETE FROM daily_reports WHERE session_id IN (...), 
       WHERE child_user_id = :child   ← 双路径汇合
```

两条 CASCADE 路径汇到同一 end state,无差异、无循环。

### 1.4 迁移

`backend/alembic/versions/<rev>_daily_report_split_content.py`:

- 单 migration,模型先改(autogenerate 拿到 DDL)
- **空表,无 backfill DML**,无失败占位逻辑
- DDL 顺序:加 6 列 → 加 `session_id` 列 + FK → 加两个索引 → drop `content`
- `downgrade()`:加回 `content Text NULL`(空表无数据)→ drop 6 列 + session_id + 两个索引

---

## 模块 2:写路径

### 2.1 `app/domain/expert/context_schema.py` — 加 `session_id` 字段

`ExpertContextSchema` 新增字段:

```python
session_id: uuid.UUID = Field(..., description="当日 chat session,expert 锚定目标")
```

worker 在构建 `ExpertContextSchema` 时,从 `ctx.child_user_id` + 当日 day_start/day_end 范围查 `chat.sessions` 拿到唯一那条 session_id 填入(1:1 前提下)。

### 2.2 `app/domain/expert/usecase.py` — write_expert_results

**现:** ON CONFLICT `(child_user_id, report_date)`,SET 仅 `overall_status` / `dimension_summary` / `content` / `degraded`。

**改后:**

```python
stmt = insert(DailyReport).values(
    child_user_id=child_user_id,
    session_id=session_id,                # 新
    report_date=report_date,
    overall_status=output.overall_status,
    dimension_summary=dimension_summary,
    today_overview=output.today_overview,    # 6 段直写
    what_was_discussed=output.what_was_discussed,
    emotion_changes=output.emotion_changes,
    noteworthy=output.noteworthy,
    suggestions=output.suggestions,
    anomaly_periods=output.anomaly_periods,
    degraded=output.degraded,
)
stmt = stmt.on_conflict_do_update(
    index_elements=[DailyReport.child_user_id, DailyReport.report_date],
    set_={
        DailyReport.session_id: stmt.excluded.session_id,    # 一起更新
        DailyReport.overall_status: stmt.excluded.overall_status,
        DailyReport.dimension_summary: stmt.excluded.dimension_summary,
        DailyReport.today_overview: stmt.excluded.today_overview,
        DailyReport.what_was_discussed: stmt.excluded.what_was_discussed,
        DailyReport.emotion_changes: stmt.excluded.emotion_changes,
        DailyReport.noteworthy: stmt.excluded.noteworthy,
        DailyReport.suggestions: stmt.excluded.suggestions,
        DailyReport.anomaly_periods: stmt.excluded.anomaly_periods,
        DailyReport.degraded: stmt.excluded.degraded,
    },
)
```

**`session_id` 进 SET 子句的理由:** 即便"1:1"被破坏(产品上不该发生),重跑 expert 时 session_id 同步成当前活跃的那条,旧 session 与 report 脱钩——不假设 1:1 永远成立,产品逻辑挡不住时 DB 不留垃圾。

**`write_expert_results` 签名变更:** 加 `session_id: uuid.UUID` 参数(从 `ctx.session_id` 透传)。

### 2.3 `app/domain/expert/graph.py:write_results` — 透传 session_id

`write_results` 节点从 `runtime.context.session_id` 取值,传给 `write_expert_results`。其他不变。

### 2.4 `app/domain/expert/worker.py` — context 构建加 session_id

`run_daily_reports` 路径上,worker 为每个 child 构建 `ExpertContextSchema` 时填 `session_id`。

**`owned_session_ids` 语义澄清:** `ctx.owned_session_ids` 是该 child 拥有的**全部 session(跨历史)**,不是当日。worker 已有 `day_start` / `day_end` 逻辑日边界,需要用它**过滤** `owned_session_ids` 得当日唯一那条。

**取法:** worker 在为该 child 构建 context 时,跨域 inline import `chat.Session`(已有先例,见 `worker.py:46` / `repository.py:122`),按 `id IN owned_session_ids AND created_at IN [day_start, day_end)` 查当日 session,分三路:

- `rows == 0`:当日无 chat session,**跳过该 child,本轮不生成 report**(产品逻辑"有聊才有报")。`run_daily_reports` 在 children 迭代层处理,本 spec 不展开。
- `len(rows) == 1`:**正常路径**,取该 `session.id` 作 `ctx.session_id`。
- `len(rows) >= 2`:**产品不变量被破坏,fail loud**——`raise RuntimeError(f"child {child_id} has {n} sessions on {date}, 1:1 invariant violated")`,被 worker 现有的 `return_exceptions=True` 接住,记 error log,跳过该 child。

代码形态(放进 worker 的 child 迭代循环):

```python
from app.domain.chat.models import Session  # 跨域 inline import,先例见 worker.py:46

async with ctx.db_session_factory() as db:
    today_sessions = (await db.execute(
        select(Session).where(
            Session.id.in_(ctx.owned_session_ids),
            Session.created_at >= ctx.day_start,
            Session.created_at < ctx.day_end,
        )
    )).scalars().all()

if len(today_sessions) == 0:
    continue   # 跳过该 child
if len(today_sessions) >= 2:
    raise RuntimeError(...)

expert_ctx = ExpertContextSchema(
    ...,
    session_id=today_sessions[0].id,
)
```

与 §1.2 两个 unique 约束的 defense-in-depth 思路一致:DB 层用 unique 约束挡,worker 层用 fail loud 挡,产品逻辑用 1:1 假设挡,三层不互依赖。

---

## 模块 3:读路径

### 3.1 `app/domain/expert/worker.py` — 删 `_parse_today_overview_from_content`

**现:** `_get_recent_reports` 拉 5 条历史 report,用 `_parse_today_overview_from_content` 切每条的 `today_overview` 字段。

**改后:**

```python
async def _get_recent_reports(db, child_user_id, exclude_date, limit=5):
    rows = (await db.execute(
        select(
            DailyReport.report_date,
            DailyReport.overall_status,
            DailyReport.today_overview,    # 直接读列,无 parse
        )
        .where(
            DailyReport.child_user_id == child_user_id,
            DailyReport.report_date < exclude_date,
        )
        .order_by(DailyReport.report_date.desc())
        .limit(limit)
    )).all()
    return [
        {
            "report_date": str(r.report_date),
            "overall_status": r.overall_status.value,
            "today_overview": r.today_overview,
        }
        for r in rows
    ]
```

`_parse_today_overview_from_content` 整段删除。下游 `load_context` 渲染"## 近期历史报告概览"的代码不变(input 形态从字符串变 3 元组 dict,组装代码自然兼容)。

### 3.2 `app/domain/expert/repository.py` — 全面 ORM 化

**7 个只读函数全部从 `text()` 迁到 ORM:**

- `search_turn_summaries`
- `search_session_notes`
- `search_crisis_topics`
- `search_daily_reports`
- `fetch_turn`
- `fetch_notes`
- `fetch_report`

**文件顶部 docstring 更新:**

- 删"全部通过 `from sqlalchemy import text` 执行原始 SQL"
- 改为"全部走 ORM(`select` + `.where()`),仅在 PG 专有语法必需时(本文件当前无)用 `text()`"

**`search_daily_reports` 关键改动:** `ILIKE ANY` 跨 6 列搜,跨 6 列 OR 拼接:

```python
from sqlalchemy import or_, any_

SIX_SECTIONS = (
    "today_overview",
    "what_was_discussed",
    "emotion_changes",
    "noteworthy",
    "suggestions",
    "anomaly_periods",
)

async def search_daily_reports(db, child_user_id, keywords, start_date, end_date, limit, context_chars, exclude_report_date=None):
    if not keywords:
        return []
    # 通配符转义
    kw_patterns = [f"%{_escape_like(kw)}%" for kw in keywords]
    sect_attrs = [getattr(DailyReport, c) for c in SIX_SECTIONS]
    stmt = (
        select(
            DailyReport.id,
            DailyReport.report_date,
            DailyReport.created_at,
            *[DailyReport.__table__.c[c] for c in SIX_SECTIONS],  # 6 列都 select 供 snippet 阶段挑
        )
        .where(
            DailyReport.child_user_id == child_user_id,
            (start_date is None) | (DailyReport.report_date >= start_date),
            (end_date is None) | (DailyReport.report_date <= end_date),
            (exclude_report_date is None) | (DailyReport.report_date != exclude_report_date),
            or_(*[attr.ilike(any_(kw_patterns), escape="\\") for attr in sect_attrs]),
        )
        .order_by(DailyReport.report_date.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    results = []
    for row in rows:
        rid = str(row.id)
        # 挑首个命中列做 snippet
        matched_text = None
        for c in SIX_SECTIONS:
            val = getattr(row, c)
            if val and any(kw.lower() in val.lower() for kw in keywords):
                matched_text = val
                break
        if matched_text is None:
            matched_text = row.today_overview   # WHERE 已过滤,理论上不进
        snippet = _extract_snippet(matched_text, keywords, context_chars)
        results.append(_make_result(
            ref=f"report:{rid}",
            source="daily_report",
            snippet=snippet,
            occurred_at=str(row.created_at) if row.created_at else None,
            matched=_match_matched(matched_text, keywords),
            locating=f"日报 {row.report_date} (id: {rid})",
        ))
    return results
```

**`fetch_report` 关键改动:** 返回结构化 dict(替代 markdown `content`)。**保持 generic,不加 child_user_id 过滤——ownership check 放在 handler 层(§4.1),跟 `fetch_turn` / `fetch_notes` 既有模式一致。**

```python
async def fetch_report(db, report_id):
    row = (await db.execute(
        select(DailyReport).where(DailyReport.id == report_id)
    )).scalar_one_or_none()
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
```

**ILIKE 通配符转义 helper**(`_extract_snippet` 同文件):

```python
def _escape_like(s: str) -> str:
    """转义 LIKE/ILIKE 通配符 \\ % _,与 SQL 端 ESCAPE '\\' 配对。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
```

`_extract_snippet` 内部:对 LLM 给的 keyword 列表先 `_escape_like` 再做子串匹配,SQL 端 `ESCAPE '\'` 配合。

### 3.3 CLAUDE.md 更新

**删**"ORM 优先"段的"允许裸 SQL 的唯一例外:PostgreSQL 专有语法(`jsonb_array_elements`、`ILIKE ANY`、`ON CONFLICT DO UPDATE` 等)"中关于 `ILIKE ANY` 的示例引用。

更新后:本项目所有只读查询走 ORM,唯一允许 `text()` 的例外是 PG JSONB 元素展开和 `ON CONFLICT DO UPDATE`(后者已在 `usecase.py` 用 `sqlalchemy.dialects.postgresql.insert` 表达,不需要 `text()`;此条规则保留以防未来需要)。

---

## 模块 4:检索工具真问题修补

### 4.1 A1:`_fetch_by_ref` 加 ownership check

**现** (`tools.py:300-312`,`kind=report` 分支):

```python
elif match.group("kind_report") is not None:
    rid = match.group("rid")
    try:
        bundle = await fetch_report(db, rid)    # ← 无 owner check
    except ValueError as exc:
        return ToolMessage(content=json.dumps({"error": str(exc)}), ...)
```

**改后(模式跟 `turn:` / `notes:` 路径一致——owner check 在 handler 层):**

```python
elif match.group("kind_report") is not None:
    rid = match.group("rid")
    try:
        bundle = await fetch_report(db, rid)
    except ValueError as exc:
        return ToolMessage(content=json.dumps({"error": str(exc)}), ...)
    if bundle is None:
        return ToolMessage(
            content=json.dumps({"error": f"report {rid} not found"}, ...),
            tool_call_id=tid,
        )
    # Owner check:report 必须属于 ctx.child_user_id
    if bundle["child_user_id"] != str(ctx.child_user_id):
        return ToolMessage(
            content=json.dumps(
                {"error": f"report {rid} not owned by child"}, ...,
            ),
            tool_call_id=tid,
        )
```

**设计取舍:** 把 owner check 放在 handler 层而不是 repository 层,跟既有 `turn:` / `notes:` 路径(`tools.py:247-254` / 281-288 用 `sid_uuid not in ctx.owned_session_ids` 校验)保持一致。`fetch_report` 维持 generic,不引入新签名。`turn:` / `notes:` 路径不动。

### 4.2 A2:`SearchHistoryInput` 改单源

**`app/domain/expert/schemas.py` 变更:**

```python
EXPERT_SEARCH_SOURCE_VALUES = ("turn_summary", "session_notes", "crisis_topic", "daily_report")

class SearchHistoryInput(BaseModel):
    keywords: list[str] = Field(..., min_length=1, ...)
    source: Literal[
        "turn_summary", "session_notes", "crisis_topic", "daily_report"
    ] = Field(..., description="单源检索;多源请多次调用")
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(default=10, ge=1, le=50)
    context_chars: int = Field(default=80, ge=0, le=400)
```

**删除** `EXPERT_SEARCH_SOURCES` 列表(用 `EXPERT_SEARCH_SOURCE_VALUES` 替代),`sources: list[str]` 字段替换为 `source: str`。

**`tools.py:_search_history` 变更:**

```python
source = validated.source   # 单值,required
results: list[dict[str, Any]] = []
async with ctx.db_session_factory() as db:
    if source == "turn_summary":
        results.extend(await search_turn_summaries(db, ...))
    elif source == "session_notes":
        results.extend(await search_session_notes(db, ...))
    elif source == "crisis_topic":
        results.extend(await search_crisis_topics(db, ...))
    elif source == "daily_report":
        results.extend(await search_daily_reports(db, ...))
# 单源后扇出逻辑消失,直接 results[:limit]
return ToolMessage(
    content=json.dumps({"results": results[:limit], "total": len(results)}),
    tool_call_id=tool_call_id,
)
```

`limit` 语义变干净:per-source 取 limit 条,LLM 想多源就多次调。

### 4.3 A4:工具 handler 装饰器 catch DB 异常

**`app/domain/expert/tools.py` 新增装饰器:**

```python
from functools import wraps
from sqlalchemy.exc import DBAPIError, ProgrammingError, ResourceClosedError

def _with_db_error_handling(handler):
    """包装 tool handler:DB 错误转 error ToolMessage,代码 bug(ProgrammingError) 照旧上抛。

    Catch 列表依据见 backend/scripts/probe_sa_asyncpg_exceptions.py
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

EXPERT_TOOL_HANDLERS: dict[str, Any] = {
    "SearchHistoryInput": _with_db_error_handling(_search_history),
    "FetchByRefInput": _with_db_error_handling(_fetch_by_ref),
}
```

**异常映射依据(锁死):**

| 故障 | SA 顶层异常 | MRO | 装饰器处理 |
|---|---|---|---|
| 杀 backend(其他 conn 杀) | `sqlalchemy.exc.InterfaceError` | `InterfaceError -> DBAPIError -> ...` | catch |
| `statement_timeout` 触发 | `sqlalchemy.exc.DBAPIError`(**基类!**) | `DBAPIError -> ...` | catch |
| `use_closed_connection` | `sqlalchemy.exc.ResourceClosedError` | `ResourceClosedError -> InvalidRequestError -> SQLAlchemyError -> ...` | catch |
| SQL 语法错 | `sqlalchemy.exc.ProgrammingError` | `ProgrammingError -> DatabaseError -> DBAPIError -> ...` | **re-raise(走 stack trace 暴露)** |
| 表不存在 | `sqlalchemy.exc.ProgrammingError` | 同上 | **re-raise** |
| UNIQUE 冲突 | `sqlalchemy.exc.IntegrityError` | `IntegrityError -> DatabaseError -> DBAPIError -> ...` | catch(ON CONFLICT 已挡,真出视为 DB 故障) |

**关键:** `InterfaceError` 与 `ResourceClosedError` 不在同一棵树上(后者在 `SQLAlchemyError` 另一支),所以 catch 列表需要 `DBAPIError + ResourceClosedError` 两类。`ProgrammingError` 必须在 `DBAPIError` 之前显式 `raise`,否则会被宽 catch 兜住——这是 Python except 顺序的硬要求。

---

## 模块 5:测试

### 5.1 `backend/scripts/probe_sa_asyncpg_exceptions.py`(新增,一次性校准)

跑一次锁死异常映射,后续 SA/asyncpg 升级时重跑对照。

```python
"""一次性 calibration probe:实测各 DB 故障场景下 SA/asyncpg 抛的异常类型。

不进入测试套件,跑完贴结果,作为 handler 异常 catch 列表的依据。
7 个场景逐个打印 outer type + MRO + root cause + message,详见 §4.3 的映射表。

跑法:docker compose exec -T api python -m backend.scripts.probe_sa_asyncpg_exceptions
"""
# 7 场景要点(commit 时落地具体代码):
# 01 normal_select          - 正常查
# 02 sql_syntax_error        - "SELEKT 1"
# 03 table_not_exist         - "SELECT * FROM nonexistent_xyz"
# 04 kill_other_backend      - conn_a 拿 pid,conn_b 杀,conn_a 再查(避自杀 race)
# 05 statement_timeout       - "SET statement_timeout=100" + "SELECT pg_sleep(1)"
# 06 unique_violation        - 临时表 PK,二次 INSERT 同值
# 07 use_closed_connection   - async with 退出后 closed_sess.execute(...)
# 每个 case 用 probe(name, coro) 包裹,print outer type + MRO + root_cause + message 头 100 字符
```

### 5.2 `tests/expert/test_tool_error_handling.py`(新增)

**4 个 case,每个触发真实 DB 故障,不在测试里 `raise X`:**

- `test_search_history_kill_backend_returns_error_tool_message`:开 conn_a 拿 pid,conn_b 杀它,conn_a 再查 → 真实 `InterfaceError` → 装饰器兜住
- `test_search_history_statement_timeout_returns_error_tool_message`:`SET statement_timeout=100`,`SELECT pg_sleep(1)` → 真实基类 `DBAPIError` → 装饰器兜住
- `test_search_history_use_closed_connection_returns_error_tool_message`:`async with factory() as closed_sess: pass` 退出后 `closed_sess.execute(...)` → 真实 `ResourceClosedError` → 装饰器兜住。**`ResourceClosedError` 不在 `DBAPIError` 树下**(在 `SQLAlchemyError` 另一支),验证必须双 catch 列表都覆盖
- `test_search_history_programming_error_propagates`:`SELEKT 1`(故意拼错)→ 真实 `ProgrammingError` → **装饰器 re-raise,不被兜住**。这条是关键——`except ProgrammingError: raise` 必须放在 `DBAPIError` 之前,否则会被宽 catch 兜住变成 bug mask

**性能估算:** 3 个 catch case 各 ~150ms(走真实 query + sleep/close),1 个 propagation case <50ms。**总 ~500ms,4 个 case,对测试套件无显著影响。**

每个 case 验证:
- catch case:返回 `ToolMessage`,`tool_call_id` 透传,payload 含自然语言 error 字段(中文"数据库"等关键字),`logger.exception` 触发,`caplog.records` 含 stack trace
- propagation case:`pytest.raises(ProgrammingError)` 验证异常透传,`caplog.records` **不**含 db_error 日志(因为走 re-raise,不走 catch 路径)

### 5.3 既有测试更新

- `tests/expert/test_repository.py`:把 fixture markdown 字符串(`## 今日概览\n\n平稳的一天\n\n## 聊了什么\n\n玩了游戏`)改为 6 列直写
- `tests/expert/test_schemas.py`:`SearchHistoryInput` 测试加 `source: str` required 校验
- `tests/expert/test_graph.py`:`expert_tools` 节点不变,tool 调用契约不变(LLM 工具名 + 入参 schema 同)
- `tests/expert/test_tools.py`:`_search_history` 跨源调用测试改为单源调用
- `tests/integration/test_smoke.py`:worker fixture 路径同步

### 5.4 conftest fixture 调整

`tests/integration/conftest.py` 已有 `db_session_factory` fixture。`test_tool_error_handling.py` 用其真实 session 触发故障,**不绕过 conftest**(CLAUDE.md 守卫)。

---

## 模块 6:涟漪更新清单

| 文件 | 变更 |
|---|---|
| `app/domain/expert/models.py` | drop `content`,加 6 列 + `session_id` + 2 索引 |
| `app/domain/expert/context_schema.py` | 加 `session_id` 字段 |
| `app/domain/expert/usecase.py` | `write_expert_results` 加 `session_id` 参数,ON CONFLICT SET 加 `session_id`,6 段直写 |
| `app/domain/expert/graph.py` | `write_results` 透传 `ctx.session_id` 给 `write_expert_results` |
| `app/domain/expert/worker.py` | 删 `_parse_today_overview_from_content`,`_get_recent_reports` 改读列;context 构建按 day_start/day_end 过滤 `owned_session_ids` 得 `session_id`(跨域 inline import `chat.Session`) |
| `app/domain/expert/repository.py` | 7 个只读函数全部 ORM 化;`search_daily_reports` 跨 6 列搜;`fetch_report` 返结构化 dict(generic,不加 owner 过滤);加 `_escape_like` |
| `app/domain/expert/tools.py` | 新增 `_with_db_error_handling` 装饰器 + `EXPERT_TOOL_HANDLERS` 套装饰器;`_search_history` 改单源;`_fetch_by_ref` `report:` 分支加 ownership check |
| `app/domain/expert/schemas.py` | `SearchHistoryInput` 改 `source: str` Literal;`EXPERT_SEARCH_SOURCES` → `EXPERT_SEARCH_SOURCE_VALUES` |
| `CLAUDE.md` | 删 `ILIKE ANY` 裸 SQL 例外 |
| `backend/alembic/versions/<rev>_daily_report_split_content.py` | 单 migration 纯 DDL(空表) |
| `backend/scripts/probe_sa_asyncpg_exceptions.py` | 新增一次性 probe |
| `tests/expert/test_repository.py` | fixture markdown 改 6 列 |
| `tests/expert/test_schemas.py` | `SearchHistoryInput` 测试更新 |
| `tests/expert/test_tools.py` | 单源调用测试;`_fetch_by_ref` `report:` 分支加 owner check 测试(用 child A 的 report_id 但 ctx 是 child B) |
| `tests/expert/test_worker.py` | 加 `session_id` 推导的 day-filter 测试:0 session skip / 1 session 正常 / ≥2 session fail loud |
| `tests/expert/test_tool_error_handling.py` | 新增 4 个真实 DB 故障 case(3 catch + 1 ProgrammingError propagation) |
| `tests/integration/test_smoke.py` | worker fixture 路径同步 |
| `tests/integration/conftest.py` | 不变(`db_session_factory` 已存在) |

---

## 风险与未决项

| 项 | 风险 | 缓解 |
|---|---|---|
| `statement_timeout` 抛基类 `DBAPIError` | 宽 catch 必须覆盖基类,否则会漏 statement_timeout → child skip | 装饰器 `except ProgrammingError: raise` 显式 re-raise,`DBAPIError` 宽 catch 兜底;bug 走 stack trace 暴露路径 |
| 加 `session_id` FK 后 child delete 双 CASCADE 路径 | 多一条 DELETE,但不改变 end state | 测试覆盖 cascade |
| `search_daily_reports` 跨 6 列 OR LIKE | 性能比单列差 | 每孩每天 1 行,扫表无压力;后续按需加 GIN/TS 索引 |
| `fetch_report` 加 `child_user_id` 过滤影响 owner 是 report 但 session 不在 owned_session_ids 边界 | 1:1 前提下不应触发 | 加测试覆盖正常 owner 路径 |
| repository.py ORM 化波及 6 个无关函数 | 改动面大 | 7 个函数改造一致(都是 `text() -> select()` 模式),autogen 不会波及;既有 `test_repository.py` 跑通即覆盖 |

---

## 验收标准

- [ ] `alembic upgrade head && alembic check` 无漂移
- [ ] `docker compose exec api ruff format && docker compose exec api ruff check && docker compose exec api basedpyright` 全过
- [ ] `docker compose exec api pytest tests/expert` 全过
- [ ] `docker compose exec api pytest tests/integration` 全过
- [ ] 端到端:跑一次 expert,生成的 daily_reports 行的 6 段列均填充正确,`session_id` 正确
- [ ] `backend/scripts/probe_sa_asyncpg_exceptions.py` 跑出的异常映射与本 spec §4.3 一致(SA 升级时重跑)
- [ ] 6 段 markdown parse 路径(grep `## 今日概览`、`_parse_today_overview_from_content`)在 repo 中已不存在
