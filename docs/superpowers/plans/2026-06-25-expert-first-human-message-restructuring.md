# Expert 首轮 HumanMessage 重组 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `expert/graph.py::load_context` 内联的首轮 HumanMessage 组装抽到 `prompts.py`(`build_expert_first_human_message`),让 `prompts.py` 与 `audit/prompts.py` 风格对齐;同步移除 `ExpertContextSchema.recent_reports_overview` 字段,把数据查询下沉到 `load_context` 节点内的私有 helper。

**Architecture:** 分三层职责对齐 —— `prompts.py` 纯文本组装(零 ORM);`graph.py::load_context` 调两个私有 helper(`_fetch_recent_reports` / `_fetch_today_materials`)取数据 + 调两个 builder(`build_expert_system_prompt` / `build_expert_first_human_message`)拼首帧;`context_schema` 不再承载 `recent_reports_overview`。ORM 行在 graph 层归一化为 TypedDict 快照,prompts.py 只接快照;`entry["turn_number"]` 直接下标访问,去除 `.get("turn", .get("turn_number"))` 死代码。

**Tech Stack:** Python 3.14 / SQLAlchemy[asyncio] / LangChain Core (`HumanMessage`) / LangGraph / pytest (asyncio_mode=auto) / ruff / basedpyright。

## Global Constraints

- 所有命令在容器内执行:`docker compose exec api <command>`,不绕过容器直接连库/连服务
- 中文注释 + Google 风格 docstring,半角标点(测试代码可放松)
- 提交信息:中文前缀 + scope,例:`refactor(expert): 抽取 build_expert_first_human_message`
- 不动 `expert/repository.py:231` 的同款 `entry.get("turn", ...)` 死代码(留 follow-up)
- 不改 system prompt / 工具契约 / 图拓扑 / ExpertReportSchema
- `prompts.py` 零 ORM 依赖(对齐 `audit/prompts.py` 仅引 `ChildProfileSnapshot` 风格)
- 章节标题 inline 在 f-string 内(对齐 `chat/prompts.py` / `audit/prompts.py`),不引入模块级常量
- `messages.role` 用 `human` / `ai`(LangChain 对齐)
- `HTTPException` 用 `from fastapi import status` + `status.HTTP_xxx_xxx` 常量
- 静态检查三道闸提交前必跑:`ruff format` / `ruff check` / `basedpyright`

---

## File Structure

| 文件 | 角色 | 改动 |
|---|---|---|
| `app/domain/expert/prompts.py` | 文本组装 | **新增** TypedDict 4 个 + `build_expert_first_human_message` |
| `app/domain/expert/graph.py` | 节点编排 | **修改** `load_context` 主体;**新增** `_fetch_recent_reports` / `_fetch_today_materials` 私有 helper |
| `app/domain/expert/context_schema.py` | 上下文契约 | **修改** 移除 `recent_reports_overview` 字段 |
| `app/domain/expert/worker.py` | cron 入口 | **修改** 删除 `_get_recent_reports` + 调用点 + ctx 构造参数 |
| `tests/expert/test_prompts.py` | 新 builder 单测 | **新增** 5 个最小覆盖 |
| `tests/expert/test_graph.py` | 图节点测试 | **修改** `_make_mock_ctx` 默认值 + 删除 `test_includes_recent_reports_in_human_message` |
| `tests/expert/test_tools.py` | 工具 handler 测试 | **修改** `_make_runtime` 默认值 |
| `tests/integration/expert/test_tool_error_handling.py` | 集成测试 | **修改** `ExpertContextSchema` 构造 |

---

## Task 1: 新增 `build_expert_first_human_message` (TDD)

**Files:**
- Create: `backend/tests/expert/test_prompts.py`
- Modify: `backend/app/domain/expert/prompts.py`

**Interfaces:**
- Consumes: 无(全新代码)
- Produces: `build_expert_first_human_message(report_date: date, recent_reports_overview: list[_RecentReportOverviewItem], today_rolling_summaries: list[_TodayRollingSummaryItem], crisis_markers: list[_CrisisMarkerItem]) -> HumanMessage`

### Step 1: 写 5 个失败单测

在 `backend/tests/expert/test_prompts.py` 新建文件:

```python
"""build_expert_first_human_message 单元测试。

5 个最小覆盖:空输入 / 仅历史 / 仅今日 / 含危机 / 全量。
无字节等价快照测试(由作者后续改文案,守了反而限制)。
"""

from __future__ import annotations

from datetime import date

from app.core.enums import DailyStatus
from app.domain.expert.prompts import build_expert_first_human_message
from langchain_core.messages import HumanMessage

REPORT_DATE = date(2026, 6, 23)


def test_empty_input_renders_only_date_header():
    """4 个参数全空 → 只渲染报告日期头。"""
    result = build_expert_first_human_message(
        report_date=REPORT_DATE,
        recent_reports_overview=[],
        today_rolling_summaries=[],
        crisis_markers=[],
    )
    assert isinstance(result, HumanMessage)
    assert result.content == f"报告日期: {REPORT_DATE.isoformat()}\n"


def test_history_overview_only_renders_history_section():
    """仅 recent_reports_overview 非空 → 渲染历史段,无今日段。"""
    recent = [
        {
            "report_date": date(2026, 6, 22),
            "overall_status": DailyStatus.stable,
            "today_overview": "正常的一天",
        },
    ]
    result = build_expert_first_human_message(
        report_date=REPORT_DATE,
        recent_reports_overview=recent,
        today_rolling_summaries=[],
        crisis_markers=[],
    )
    assert "## 近期历史报告概览" in result.content
    assert "2026-06-22 [stable]: 正常的一天" in result.content
    assert "## 今日对话材料" not in result.content
    assert "## 危机标记" not in result.content


def test_today_materials_only_renders_today_section():
    """仅 today_rolling_summaries 非空 → 渲染今日段(含 session + notes)。"""
    today = [
        {
            "session_id": "sess-abc",
            "turn_summaries": [
                {"turn_number": 1, "summary": "聊了学校"},
                {"turn_number": 2, "summary": "聊了游戏"},
            ],
            "session_notes": "情绪稳定",
        },
    ]
    result = build_expert_first_human_message(
        report_date=REPORT_DATE,
        recent_reports_overview=[],
        today_rolling_summaries=today,
        crisis_markers=[],
    )
    assert "## 今日对话材料" in result.content
    assert "### Session: sess-abc" in result.content
    assert "- Turn 1: 聊了学校" in result.content
    assert "- Turn 2: 聊了游戏" in result.content
    assert "会话笔记 (sess-abc):" in result.content
    assert "情绪稳定" in result.content
    assert "## 危机标记" not in result.content


def test_crisis_markers_rendered_when_present():
    """crisis_markers 非空 + today_rolling_summaries 也非空 → 危机子段渲染。"""
    today = [
        {
            "session_id": "sess-abc",
            "turn_summaries": [{"turn_number": 1, "summary": "聊了学校"}],
            "session_notes": "",
        },
    ]
    crisis = [
        {"session_id": "sess-abc", "turn_number": 3, "crisis_topic": "情绪低落"},
    ]
    result = build_expert_first_human_message(
        report_date=REPORT_DATE,
        recent_reports_overview=[],
        today_rolling_summaries=today,
        crisis_markers=crisis,
    )
    assert "## 今日对话材料" in result.content
    assert "## 危机标记" in result.content
    assert "- Session sess-abc, Turn 3: 情绪低落" in result.content


def test_full_data_renders_all_four_sections_in_order():
    """全量数据 → 日期头 → 历史 → 今日(含危机) 4 段按固定顺序。"""
    recent = [
        {
            "report_date": date(2026, 6, 22),
            "overall_status": DailyStatus.alert,
            "today_overview": "前一天有危机",
        },
    ]
    today = [
        {
            "session_id": "sess-xyz",
            "turn_summaries": [{"turn_number": 1, "summary": "恢复中"}],
            "session_notes": "",
        },
    ]
    crisis = [
        {"session_id": "sess-xyz", "turn_number": 5, "crisis_topic": "低落"},
    ]
    result = build_expert_first_human_message(
        report_date=REPORT_DATE,
        recent_reports_overview=recent,
        today_rolling_summaries=today,
        crisis_markers=crisis,
    )
    content = result.content
    # 4 段顺序
    assert content.index("报告日期:") < content.index("## 近期历史报告概览")
    assert content.index("## 近期历史报告概览") < content.index("## 今日对话材料")
    assert content.index("## 今日对话材料") < content.index("## 危机标记")
    # 4 段都渲染
    assert "2026-06-22 [alert]: 前一天有危机" in content
    assert "### Session: sess-xyz" in content
    assert "- Turn 1: 恢复中" in content
    assert "- Session sess-xyz, Turn 5: 低落" in content
```

### Step 2: 跑测试,验证失败

```bash
docker compose exec api pytest tests/expert/test_prompts.py -v
```

Expected: 5 errors,`ImportError: cannot import name 'build_expert_first_human_message' from 'app.domain.expert.prompts'`。

### Step 3: 实现 TypedDict + `build_expert_first_human_message`

修改 `backend/app/domain/expert/prompts.py`,在文件**末尾追加**(不动现有 `build_expert_system_prompt`):

```python
# 追加在文件末尾,保留原 build_expert_system_prompt 不变

from datetime import date
from typing import TypedDict

from langchain_core.messages import HumanMessage

from app.core.enums import DailyStatus


class _RecentReportOverviewItem(TypedDict):
    """近期历史报告概览(由 _fetch_recent_reports 产出)。"""
    report_date: date
    overall_status: DailyStatus
    today_overview: str


class _TurnSummaryItem(TypedDict):
    """单轮对话摘要(由 ORM 行 turn_summaries JSONB 元素归一化)。"""
    turn_number: int
    summary: str


class _TodayRollingSummaryItem(TypedDict):
    """单 session 的滚动摘要。"""
    session_id: str
    turn_summaries: list[_TurnSummaryItem]
    session_notes: str


class _CrisisMarkerItem(TypedDict):
    """单条危机标记(从 AuditRecord.crisis_detected=True 行映射)。"""
    session_id: str
    turn_number: int
    crisis_topic: str


def build_expert_first_human_message(
    report_date: date,
    recent_reports_overview: list[_RecentReportOverviewItem],
    today_rolling_summaries: list[_TodayRollingSummaryItem],
    crisis_markers: list[_CrisisMarkerItem],
) -> HumanMessage:
    """组装首轮 HumanMessage。

    章节结构(顺序固定,空段跳过):
      1. 报告日期头(恒渲染)
      2. ## 近期历史报告概览(recent_reports_overview 非空时)
      3. ## 今日对话材料(today_rolling_summaries 非空时,内含 session 块)
      4. ## 危机标记(today_rolling_summaries 非空且 crisis_markers 非空时)
    """
    parts: list[str] = [f"报告日期: {report_date.isoformat()}", ""]

    if recent_reports_overview:
        parts.append("## 近期历史报告概览")
        for overview in recent_reports_overview:
            rd = overview["report_date"].isoformat()
            st = overview["overall_status"].value
            ov = overview["today_overview"]
            parts.append(f"- {rd} [{st}]: {ov}")
        parts.append("")

    if today_rolling_summaries:
        parts.append("## 今日对话材料")
        for rsid in today_rolling_summaries:
            turn_summaries = rsid["turn_summaries"]
            if turn_summaries:
                parts.append(f"### Session: {rsid['session_id']}")
                for entry in turn_summaries:
                    parts.append(f"- Turn {entry['turn_number']}: {entry['summary']}")
                parts.append("")

            if rsid["session_notes"]:
                parts.append(f"会话笔记 ({rsid['session_id']}):")
                parts.append(rsid["session_notes"])
                parts.append("")

        if crisis_markers:
            parts.append("## 危机标记")
            for cm in crisis_markers:
                parts.append(
                    f"- Session {cm['session_id']}, "
                    f"Turn {cm['turn_number']}: {cm['crisis_topic']}"
                )
            parts.append("")

    return HumanMessage(content="\n".join(parts))
```

注意:把 `from datetime import date` / `from typing import TypedDict` / `from langchain_core.messages import HumanMessage` / `from app.core.enums import DailyStatus` 与文件现有 import 合并;不要重复 import。

### Step 4: 跑测试,验证通过

```bash
docker compose exec api pytest tests/expert/test_prompts.py -v
```

Expected: 5 passed。

### Step 5: ruff + basedpyright 检查

```bash
docker compose exec api ruff format app/domain/expert/prompts.py tests/expert/test_prompts.py
docker compose exec api ruff check app/domain/expert/prompts.py tests/expert/test_prompts.py
docker compose exec api basedpyright app/domain/expert/prompts.py tests/expert/test_prompts.py
```

Expected: 无 error/warning。

### Step 6: 提交

```bash
git add app/domain/expert/prompts.py tests/expert/test_prompts.py
git commit -m "feat(expert): 抽取 build_expert_first_human_message 到 prompts.py

- 新增 4 个私有 TypedDict(同文件):_RecentReportOverviewItem /
  _TurnSummaryItem / _TodayRollingSummaryItem / _CrisisMarkerItem
- 字段保留原格式:date / DailyStatus / int
- 章节 inline 在 f-string 内,对齐 chat/audit prompts.py 风格
- 5 个最小单测覆盖:空输入 / 仅历史 / 仅今日 / 含危机 / 全量
- 不做字节等价快照(作者后续改文案,守了反而限制)"
```

---

## Task 2: graph.py 私有 helper 提取 + load_context 重构

**Files:**
- Modify: `backend/app/domain/expert/graph.py`

**Interfaces:**
- Consumes: `build_expert_first_human_message`(Task 1)
- Produces: `_fetch_recent_reports(db_session_factory, child_user_id, exclude_date, limit=5) -> list[_RecentReportOverviewItem]`;`_fetch_today_materials(db_session_factory, owned_session_ids, day_start, day_end) -> tuple[list[_TodayRollingSummaryItem], list[_CrisisMarkerItem]]`;重构后的 `load_context`

### Step 1: 在 `graph.py` 顶部加 import

修改 `backend/app/domain/expert/graph.py` 顶部 import 区。两处需要改:

**A. 把现有 `from app.domain.expert.prompts import build_expert_system_prompt`(原 graph.py:33)扩成 tuple 形式,加 `build_expert_first_human_message`:**

```python
from app.domain.expert.prompts import (
    build_expert_first_human_message,
    build_expert_system_prompt,
)
```

**B. 在文件顶部新增(用于私有 helper 的类型注解):**

```python
import uuid
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
```

注意:这些类型注解在 helper 函数签名里用到(`uuid.UUID` / `date` / `datetime` / `async_sessionmaker[AsyncSession]`)。现有 graph.py 内部并不直接 import 这些(只通过 `ctx.xxx` 间接使用),本步需要补齐。`if TYPE_CHECKING` 块下若已有同类型 import,合并去重。

### Step 2: 在 graph.py 加 `_fetch_recent_reports` 私有 helper

在 `_build_degraded_output` 函数之后、`load_context` 函数之前,插入:

```python
async def _fetch_recent_reports(
    db_session_factory: async_sessionmaker[AsyncSession],
    child_user_id: uuid.UUID,
    exclude_date: date,
    limit: int = 5,
) -> list[_RecentReportOverviewItem]:
    """查近 limit 条历史报告概要(原 worker._get_recent_reports,移入 graph.py)。

    Args:
        db_session_factory: DB 会话工厂。
        child_user_id: 孩子用户 ID。
        exclude_date: 报告日期(不包含)。
        limit: 返回上限,默认 5。

    Returns:
        list[dict],每项含 report_date / overall_status / today_overview。
        无数据时返回空 list。
    """
    from app.domain.expert.models import DailyReport

    async with db_session_factory() as db:
        stmt = (
            select(
                DailyReport.report_date,
                DailyReport.overall_status,
                DailyReport.today_overview,
            )
            .where(
                DailyReport.child_user_id == child_user_id,
                DailyReport.report_date < exclude_date,
            )
            .order_by(DailyReport.report_date.desc())
            .limit(limit)
        )
        rows = (await db.execute(stmt)).all()
    return [
        {
            "report_date": r.report_date,
            "overall_status": r.overall_status,
            "today_overview": r.today_overview,
        }
        for r in rows
    ]
```

### Step 3: 加 `_fetch_today_materials` 私有 helper

紧接 `_fetch_recent_reports` 之后插入:

```python
async def _fetch_today_materials(
    db_session_factory: async_sessionmaker[AsyncSession],
    owned_session_ids: frozenset[uuid.UUID],
    day_start: datetime,
    day_end: datetime,
) -> tuple[list[_TodayRollingSummaryItem], list[_CrisisMarkerItem]]:
    """查今日对话材料:rolling_summaries + crisis 标记。

    前置条件:owned_session_ids 非空(由 load_context 守门,本 helper 不再二次检查)。

    Args:
        db_session_factory: DB 会话工厂。
        owned_session_ids: 该孩子所有 session ID 白名单,调用方保证非空。
        day_start: 逻辑日窗口起始(tz-aware)。
        day_end: 逻辑日窗口结束(tz-aware)。

    Returns:
        (today_summaries, crisis_markers) 元组。无数据时两个 list 都为空。
    """
    from app.domain.audit.models import AuditRecord, RollingSummary

    async with db_session_factory() as db:
        rs_rows = (
            (await db.execute(
                select(RollingSummary).where(
                    RollingSummary.session_id.in_(owned_session_ids),
                )
            ))
            .scalars()
            .all()
        )
        ar_rows = (
            (await db.execute(
                select(AuditRecord)
                .where(
                    AuditRecord.session_id.in_(owned_session_ids),
                    AuditRecord.created_at >= day_start,
                    AuditRecord.created_at < day_end,
                )
                .order_by(AuditRecord.created_at)
            ))
            .scalars()
            .all()
        )

    today_summaries: list[_TodayRollingSummaryItem] = [
        {
            "session_id": str(rsid.session_id),
            "turn_summaries": [
                {
                    "turn_number": int(entry["turn_number"]),
                    "summary": entry.get("summary", ""),
                }
                for entry in (rsid.turn_summaries or [])
            ],
            "session_notes": rsid.session_notes or "",
        }
        for rsid in rs_rows
    ]

    crisis_markers: list[_CrisisMarkerItem] = [
        {
            "session_id": str(ar.session_id),
            "turn_number": ar.turn_number,
            "crisis_topic": ar.crisis_topic,
        }
        for ar in ar_rows
        if ar.crisis_detected
    ]
    return today_summaries, crisis_markers
```

### Step 4: 重构 `load_context` 函数体

**完整替换** `backend/app/domain/expert/graph.py` 中原 `load_context` 函数(原 graph.py:126-236)为:

```python
async def load_context(
    state: ExpertGraphState,
    runtime: Runtime[ExpertContextSchema],
) -> dict:
    """构造首帧 messages: system prompt + 含材料的 HumanMessage。

    Invariant:本节点被调用时 ctx.owned_session_ids 必非空
    (worker._report_for_child 在 today_sessions 为空时已早退,
    而 owned_session_ids 包含今日 session)。一旦发现空,记 error
    日志并短路,不开 DB session、不调 _fetch_today_materials。

    Args:
        state: 当前图状态(空,首次运行)。
        runtime: LangGraph Runtime,context 即 ExpertContextSchema。

    Returns:
        含 messages(首帧 system + human)与其余状态字段的 dict。
    """
    ctx = runtime.context

    # 1. 抓数据(helper 内起短 DB session)
    recent_reports = await _fetch_recent_reports(
        ctx.db_session_factory, ctx.child_user_id, ctx.report_date,
    )

    if not ctx.owned_session_ids:
        # 生产路径不应触发,留 error 日志便于追源
        logger.error(
            "expert.load_context.empty_owned_sessions child=%s report_date=%s",
            ctx.child_user_id, ctx.report_date,
        )
        today_summaries: list[_TodayRollingSummaryItem] = []
        crisis_markers: list[_CrisisMarkerItem] = []
    else:
        today_summaries, crisis_markers = await _fetch_today_materials(
            ctx.db_session_factory, ctx.owned_session_ids, ctx.day_start, ctx.day_end,
        )

    # 2. 拼首帧
    return {
        "messages": [
            build_expert_system_prompt(ctx.max_output_attempts),
            build_expert_first_human_message(
                report_date=ctx.report_date,
                recent_reports_overview=recent_reports,
                today_rolling_summaries=today_summaries,
                crisis_markers=crisis_markers,
            ),
        ],
        "output_attempts": 0,
        "total_output_tokens": 0,
        "structured_output": None,
        "_budget_forced": False,
    }
```

注意:此步**暂不**从 `ExpertContextSchema` 移除 `recent_reports_overview` 字段(Task 3 移除);也**暂不**从 `worker.py` 移除 `_get_recent_reports`(Task 4 移除);也**暂不**改 3 个测试文件(Task 5 改)。本步目标:让重构后的 `load_context` 能用现有 ctx 形状跑通。

但因为 `recent_reports_overview` 在 `load_context` 中已不再被访问(Task 2 之后),传进来也不会被用。可以暂时让测试 fixture 继续传 `recent_reports_overview=[]`,不会破坏。

### Step 5: 跑现有 test_graph.py,验证无回归

```bash
docker compose exec api pytest tests/expert/test_graph.py -v
```

Expected: 全部通过(`test_includes_recent_reports_in_human_message` 除外 —— 它现在会失败,因为 `load_context` 不再读 `ctx.recent_reports_overview`,而是从 DB 查,DB mock 返回空)。这个失败留到 Task 5 修。

临时方案:在跑测试时用 `--deselect` 跳过该测试:

```bash
docker compose exec api pytest tests/expert/test_graph.py -v --deselect tests/expert/test_graph.py::TestLoadContextNode::test_includes_recent_reports_in_human_message
```

Expected: 其它全部通过。

### Step 6: ruff + basedpyright 检查

```bash
docker compose exec api ruff format app/domain/expert/graph.py
docker compose exec api ruff check app/domain/expert/graph.py
docker compose exec api basedpyright app/domain/expert/graph.py
```

Expected: 无 error/warning。

### Step 7: 提交

```bash
git add app/domain/expert/graph.py
git commit -m "refactor(expert): load_context 拆 helper + 改调 build_expert_first_human_message

- 新增 _fetch_recent_reports / _fetch_today_materials 模块私有 helper
- load_context 不再做字符串拼接,只调 helper + builder
- 加 owned_session_ids 守门:生产不应触发,空时 error 日志 + 短路
- 移除节点内 inline DB import,移入 helper 局部
- entry['turn_number'] 直接下标,去除 .get('turn') 死代码
- 暂未移除 ctx.recent_reports_overview 字段(下个 task 删)"
```

---

## Task 3: 从 `ExpertContextSchema` 移除 `recent_reports_overview`

**Files:**
- Modify: `backend/app/domain/expert/context_schema.py`

**Interfaces:**
- Consumes: 无
- Produces: `ExpertContextSchema` 字段集(去掉 `recent_reports_overview`)

### Step 1: 删字段 + 改 docstring

修改 `backend/app/domain/expert/context_schema.py`,删字段定义和 docstring 段。

**删除 line 42-44(原 docstring 中关于 recent_reports_overview 的整段):**

```python
        recent_reports_overview: 近 N 天历史报告摘要列表,每项含
            {report_date, overall_status, today_overview},建图前查询,
            load_context 嵌入 prompt。
```

**删除 line 66 字段定义:**

```python
    recent_reports_overview: list[dict]  # 近 N 天历史报告摘要
```

### Step 2: 跑 test_prompts.py 验证不影响

```bash
docker compose exec api pytest tests/expert/test_prompts.py tests/expert/test_graph.py -v --deselect tests/expert/test_graph.py::TestLoadContextNode::test_includes_recent_reports_in_human_message
```

Expected: 全部通过(本步没改 `load_context` 调用,新 builder 自测已覆盖;test_graph.py 其他测试用 `_make_mock_ctx` 默认值传 `recent_reports_overview=[]`,本步删字段后这些测试会报 `TypeError: unexpected keyword argument`,这要在 Task 5 修)。

如果想分阶段验证,可以用 `git stash` 暂存 Step 1 改动,跑测试看是否只有字段相关报错,再 `git stash pop` 恢复。

### Step 3: 跑 test_graph.py 验证 ctx 构造点(预期有 TypeError,留给 Task 5)

```bash
docker compose exec api pytest tests/expert/test_graph.py -v --co
```

Expected: collection 阶段就会因为 `_make_mock_ctx` 传 `recent_reports_overview=[]` 报 TypeError。

### Step 4: ruff + basedpyright 检查

```bash
docker compose exec api ruff format app/domain/expert/context_schema.py
docker compose exec api ruff check app/domain/expert/context_schema.py
docker compose exec api basedpyright app/domain/expert/context_schema.py
```

Expected: 无 error/warning。

### Step 5: 提交

```bash
git add app/domain/expert/context_schema.py
git commit -m "refactor(expert): 移除 ExpertContextSchema.recent_reports_overview 字段

- 字段下沉到 load_context 内 _fetch_recent_reports helper
- 生产代码仅 load_context 一处消费者,字段无存在必要
- docstring 同步清理
- 3 个测试文件 _make_mock_ctx 默认值待下个 task 同步删除"
```

---

## Task 4: 清理 `worker.py` —— 删 `_get_recent_reports` + 调用点 + ctx 构造参数

**Files:**
- Modify: `backend/app/domain/expert/worker.py`

**Interfaces:**
- Consumes: 无
- Produces: `worker.py` 不再依赖 `_get_recent_reports`,`ExpertContextSchema` 构造不再传 `recent_reports_overview`

### Step 1: 删 `_get_recent_reports` 函数

**删除** `backend/app/domain/expert/worker.py` line 116-156 整段函数定义 `_get_recent_reports`。

### Step 2: 删 `_report_for_child` 内的调用块

**删除** `backend/app/domain/expert/worker.py` line 285-291(原注释 + 调用):

```python
                # e. recent_reports_overview
                recent_reports = await _get_recent_reports(
                    child_db,
                    child_user_id_val,
                    report_date,
                )
```

### Step 3: 删 `ExpertContextSchema` 构造参数

**删除** `backend/app/domain/expert/worker.py` line 302:

```python
                    recent_reports_overview=recent_reports,
```

### Step 4: 检查无悬空 import

读 `worker.py` 顶部,确认 `uuid` / `date` / 其他 import 仍被使用;如有未使用的 import,删之(基于 ruff `F401` 检测)。

### Step 5: ruff + basedpyright 检查

```bash
docker compose exec api ruff format app/domain/expert/worker.py
docker compose exec api ruff check app/domain/expert/worker.py
docker compose exec api basedpyright app/domain/expert/worker.py
```

Expected: 无 error/warning。

### Step 6: 提交

```bash
git add app/domain/expert/worker.py
git commit -m "refactor(expert): worker 删 _get_recent_reports + 调用点

- 助手函数下沉到 graph.py 私有 helper,worker 不再关心
- _report_for_child 内的 e. recent_reports_overview 调用块删除
- ExpertContextSchema 构造参数 recent_reports_overview 删除
- 残留未用 import 按 ruff F401 同步清理"
```

---

## Task 5: 更新 3 个测试文件,删除 `recent_reports_overview=[]` fixture 参数

**Files:**
- Modify: `backend/tests/expert/test_graph.py`
- Modify: `backend/tests/expert/test_tools.py`
- Modify: `backend/tests/integration/expert/test_tool_error_handling.py`

**Interfaces:**
- Consumes: 上一 task 删除了 `ExpertContextSchema.recent_reports_overview` 字段
- Produces: 3 个测试文件的 `ExpertContextSchema` 构造不再传 `recent_reports_overview`

### Step 1: 改 `tests/expert/test_graph.py`

读 `backend/tests/expert/test_graph.py`,找到以下位置并修改:

**A. `_make_mock_ctx` 函数(原 line 117-135)** —— 删 `recent_reports_overview=[]`:

```python
    defaults = dict(
        child_user_id=CUID,
        owned_session_ids=frozenset({SID}),
        session_id=SID,
        report_date=REPORT_DATE,
        day_start=day_start,
        day_end=day_end,
        dimension_summary={},
        # 删 recent_reports_overview=[],  ← 此行删除
        crisis_detected_today=False,
        max_output_attempts=3,
        token_budget=100_000,
        child_profile=MagicMock(),
        settings=MagicMock(),
        db_session_factory=MagicMock(return_value=_make_mock_db_cm()),
        shared_http_client=MagicMock(),
    )
```

**B. 删除整个 `test_includes_recent_reports_in_human_message` 测试(原 line 217-228)** —— 该测试在新结构下断言"ctx.recent_reports_overview 决定 HumanMessage 内容",而新结构是"DB 查询决定 HumanMessage 内容",断言契约变了。该行为已被 `tests/expert/test_prompts.py::test_history_overview_only_renders_history_section` 覆盖,直接删即可。

```python
    # 整个 test_includes_recent_reports_in_human_message 方法删除
    # 替代覆盖:tests/expert/test_prompts.py::test_history_overview_only_renders_history_section
```

### Step 2: 改 `tests/expert/test_tools.py`

读 `backend/tests/expert/test_tools.py`,找到 `_make_runtime` / `ExpertContextSchema(...)` 构造点(原 line 38 附近)。

**删除 `recent_reports_overview=[]` 行**。如果该文件中没有更上层的覆盖该渲染逻辑的测试,无需新增(已被 test_prompts.py 覆盖)。

### Step 3: 改 `tests/integration/expert/test_tool_error_handling.py`

读 `backend/tests/integration/expert/test_tool_error_handling.py`,找到 `ExpertContextSchema` 构造点(原 line 57 附近)。

**删除 `recent_reports_overview=[]` 行**。此文件测试工具 handler 错误处理,无渲染逻辑覆盖需求,删字段即可。

### Step 4: 跑全量 expert 测试

```bash
docker compose exec api pytest tests/expert/ tests/integration/expert/ -v
```

Expected: 全部通过。

### Step 5: ruff + basedpyright 检查

```bash
docker compose exec api ruff format tests/expert/ tests/integration/expert/
docker compose exec api ruff check tests/expert/ tests/integration/expert/
docker compose exec api basedpyright tests/expert/ tests/integration/expert/
```

Expected: 无 error/warning。

### Step 6: 提交

```bash
git add tests/expert/test_graph.py tests/expert/test_tools.py tests/integration/expert/test_tool_error_handling.py
git commit -m "test(expert): 删除 3 测试文件 ExpertContextSchema.recent_reports_overview 字段引用

- test_graph.py _make_mock_ctx 默认值同步删除
- test_graph.py::test_includes_recent_reports_in_human_message 删除
  (覆盖已迁至 test_prompts.py::test_history_overview_only)
- test_tools.py / test_tool_error_handling.py 同步删字段参数"
```

---

## Task 6: 最终验证

**Files:** 无改动,只跑命令

### Step 1: 全量 expert + integration 测试

```bash
docker compose exec api pytest tests/expert/ tests/integration/expert/ -v
```

Expected: 全部通过。

### Step 2: ruff format

```bash
docker compose exec api ruff format
```

Expected: 无 diff(或仅有空白字符调整)。

### Step 3: ruff check

```bash
docker compose exec api ruff check
```

Expected: 无 error。

### Step 4: basedpyright

```bash
docker compose exec api basedpyright
```

Expected: 无 error。

### Step 5: 全量测试兜底

```bash
docker compose exec api pytest -v
```

Expected: 全部通过(若有无关失败,在 PR 描述里说明,但本任务范围不应引入新失败)。

### Step 6: 跑完无需 commit

如果上述有 diff/fixup,单独 commit(粒度由具体 diff 决定);无 diff 则本任务收尾。

---

## 边界情况速查

| 场景 | 行为 | 验证位置 |
|---|---|---|
| `recent_reports_overview=[]` | 跳过 `## 近期历史报告概览` 段 | `test_prompts.py::test_empty_input` |
| `today_rolling_summaries=[]` | 跳过 `## 今日对话材料` 段 | `test_prompts.py::test_empty_input` + `test_graph.py::test_no_owned_sids_skips_db` |
| `crisis_markers=[]` | 跳过 `## 危机标记` 段 | `test_prompts.py::test_today_materials_only` |
| 单个 session `turn_summaries=[]` 且 `session_notes=""` | 不渲染该 session 块(无 `### Session`、无 notes),但 `## 今日对话材料` 标题仍渲染 | 不做专门测试(空 session 实际不会发生) |
| `entry["turn_number"]` 缺字段 | `KeyError`,节点异常上抛 → arq `return_exceptions=True` 兜底记 error log | 不做测试(数据契约保证) |
| `owned_session_ids` 为空 | `load_context` 记 error 日志并短路,首帧 human 仅含日期头 | `test_graph.py::test_no_owned_sids_skips_db`(现有) |
