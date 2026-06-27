# Expert 首轮 HumanMessage 重组 — 设计

## 背景

`expert/graph.py::load_context` 当前**内联组装**首轮 HumanMessage:4 段(报告日期头 / 近期历史报告概览 / 今日对话材料 / 危机标记)拼在一个 `parts: list[str]` 里,然后 `"\n".join(parts)` 包成 `HumanMessage`。同一文件里 `build_expert_system_prompt(max_output_attempts)` 已经抽到 `prompts.py`,系统提示词与首轮人类消息**职责分离不对称**——system 是纯函数,human 是节点内联大块字符串。

同步发现的两个相关问题:

1. **`ExpertContextSchema.recent_reports_overview` 是 `list[dict]`(松类型),且只有 `load_context` 一处消费者**。从 ctx 上下文形状来看属于"早聚合",但近期报告数据形态简单,完全可以由 `load_context` 节点自取,省一个 ctx 字段。
2. **现有 `entry.get("turn", entry.get("turn_number", ""))` 是死代码**:`audit.TurnSummaryEntry` 字段名是 `turn_number`,DB 里所有真实数据都长这样,`"turn"` 分支永不被命中。`expert/repository.py:231` 也有同款死代码(本次不动,留 follow-up)。

本次重构把首轮 HumanMessage 的字符串组装从 `load_context` 抽到 `prompts.py`,**结构、职责、静态可分析性**三层对齐,具体文案由作者后续手工微调。

## 目标

1. **结构对称**:`prompts.py` 同时承载 system 与首轮 human 两条 builder,`load_context` 不再做字符串拼接
2. **职责分明**:`prompts.py` 零 ORM 依赖(对齐 `audit/prompts.py` 仅引 `ChildProfileSnapshot`),`load_context` 只调 helper + builder
3. **数据契约清晰**:ORM 行在 `load_context` 内归一化为 TypedDict 快照,`prompts.py` 只接快照;字段保留原类型(`date` / `enum` / `int`),`prompts.py` 内部做格式化
4. **静态可分析**:`entry["turn_number"]` 直接下标访问,缺字段静态可见(`get("x")` 静态放行运行时才炸)
5. **invariant 显式守门**:`owned_session_ids` 在生产路径下永不为空(worker 守门),`load_context` 守门检测到空时记 error 日志并短路

## 非目标

- 不改 system prompt 内容与结构
- 不改 ExpertReportSchema / 工具契约 / LangGraph 图拓扑
- 不改 audit pipeline 写入的 `turn_summaries` JSONB key(保持 `turn_number`)
- 不修 `expert/repository.py:231` 的同款死代码(留作 follow-up)
- 不做"输出与旧实现字节等价"的快照测试(用户后续会改文案,守了反而限制)
- 不在 `prompts.py` 引入章节级常量(对齐 `chat/prompts.py` / `audit/prompts.py` 的 f-string 内联风格,作者后续在 f-string 内看整体效果比分散到常量更直观)

---

## 决策记录

| 决策 | 选项 | 选定 | 理由 |
|---|---|---|---|
| 重构范围 | (a) 只迁移组装逻辑 (b) 同时上移数据查询到 worker (c) 只重组内容 | **(a)** | 与 audit 域 system 抽取同模式;DB 查询仍走 ctx.db_session_factory 短块,节点可移植 |
| prompts.py 粒度 | (a) 单一主函数 (b) 一函数一段 (c) 三个独立公共函数 | **(a) 单一主函数** | 简化 load_context 调用,作者后续只改一个 f-string 即可 |
| 函数命名 | (a) `build_expert_first_human_message` (b) `build_expert_user_prompt` (c) `build_expert_human_message` | **(a)** | 与 `build_expert_system_prompt` 词面对仗;`_first_` 明确"首轮"语义 |
| 函数入参类型 | (a) TypedDict 快照 (b) 裸 ORM 行 (c) 预拼字符串片段 | **(a)** | prompts.py 零 ORM 依赖;与 audit/prompts.py 引 ChildProfileSnapshot 风格一致 |
| 函数返回类型 | (a) `HumanMessage` (b) `str` | **(a) `HumanMessage`** | 与 `build_expert_system_prompt` 返回 `SystemMessage` 对称,load_context 调用点直接拼入 messages |
| TypedDict 位置 | (a) 同 prompts.py 私有 (b) 拆 types.py (c) 复用 audit 已有 | **(a) 同文件私有** | 重构作用域小,prompts.py 是这类数据契约的天然归属 |
| `recent_reports_overview` 在 ctx 中的去留 | (a) 保留 (b) 移除,迁入 `load_context` 内 helper | **(b) 移除** | 生产代码仅 `load_context` 一处读;3 个测试文件机械改 fixture;简化 ctx 形状 |
| `_RecentReportOverviewItem` 字段类型 | (a) `str` 预序列化 (b) `date` / `DailyStatus` 原格式 | **(b) 原格式** | prompts.py 内部用 `.isoformat()` / `.value` 格式化,职责不外溢到 graph 层 |
| `_TurnSummaryItem` 字段名 | (a) `turn` (b) `turn_number` | **(b) `turn_number`** | 对齐 `audit.TurnSummaryEntry.turn_number`;`.get("turn", .get("turn_number"))` 死代码模式去除 |
| `entry` 访问方式 | (a) `entry.get("turn_number", default)` (b) `entry["turn_number"]` | **(b) 直接下标** | 静态分析能从 `["x"]` 提前发现缺字段问题;`get("x")` 静态放行运行才炸 |
| `owned_session_ids` 为空处理 | (a) helper 内短路 (b) `load_context` 守门 + error 日志 | **(b)** | worker 守门保证生产路径下非空;`load_context` 守门提供可观测性;helper 假定前置条件满足,逻辑单点 |
| 章节空段处理 | (a) 跳过 (b) 渲染占位 | **(a) 跳过(基线)** | 改"显示空段占位"是文案层修改,在 prompts.py 内部加 if/else,不影响 graph 层 |
| 章节标题字符串位置 | (a) 模块级常量 (b) inline 在 f-string | **(b) inline** | 对齐 chat/audit prompts.py 风格;作者在 f-string 内看到整体结合效果 |
| 新函数单元测试范围 | (a) 5 个最小覆盖 (b) 5 个 + 字节等价快照 | **(a) 5 个最小覆盖** | 不加快照:作者后续会改文案,快照守会反过来限制 |

---

## 模块 1:`prompts.py` — 新增 `build_expert_first_human_message`

### 1.1 新增 TypedDict(同文件私有)

```python
from datetime import date
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.enums import DailyStatus


class _RecentReportOverviewItem(TypedDict):
    """近期历史报告概览(由 _fetch_recent_reports 产出)。"""
    report_date: date          # 原始 date,prompts.py 内 .isoformat()
    overall_status: DailyStatus  # 原始 enum,prompts.py 内 .value
    today_overview: str


class _TurnSummaryItem(TypedDict):
    """单轮对话摘要(由 ORM 行 turn_summaries JSONB 元素归一化)。"""
    turn_number: int           # 对齐 audit.TurnSummaryEntry.turn_number
    summary: str


class _TodayRollingSummaryItem(TypedDict):
    """单 session 的滚动摘要。"""
    session_id: str            # UUID 字符串
    turn_summaries: list[_TurnSummaryItem]
    session_notes: str         # 无内容用空串


class _CrisisMarkerItem(TypedDict):
    """单条危机标记(从 AuditRecord.crisis_detected=True 行映射)。"""
    session_id: str
    turn_number: int
    crisis_topic: str
```

### 1.2 新增 `build_expert_first_human_message`

返回 `HumanMessage`,与 `build_expert_system_prompt` 返回 `SystemMessage` 对称。**章节标题 inline 在 f-string 内**(对齐 `chat/prompts.py` / `audit/prompts.py` 风格)。

**结构(伪代码,具体文案由作者后续手调):**

```python
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
      4. ## 危机标记(crisis_markers 非空时,作为今日材料的子段或平级段)
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

**实现约束**:

- prompts.py **不 import** 任何 ORM / 业务模型,只引 `langchain_core.messages` / `app.core.enums` / `typing` / `datetime`
- 不在文件外暴露 TypedDict 类型(下划线前缀),`build_expert_first_human_message` 是唯一公共 API
- 同一文件内的 `build_expert_system_prompt` 不动

---

## 模块 2:`graph.py` — `load_context` 重构 + 私有 helper

### 2.1 `load_context` 新版

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

### 2.2 新增模块私有 helper

```python
async def _fetch_recent_reports(
    db_session_factory: async_sessionmaker[AsyncSession],
    child_user_id: uuid.UUID,
    exclude_date: date,
    limit: int = 5,
) -> list[_RecentReportOverviewItem]:
    """查近 limit 条历史报告概要(原 worker._get_recent_reports,移入 graph.py)。"""
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
    # 保留原格式:date / DailyStatus 不预先序列化为 str
    return [
        {
            "report_date": r.report_date,
            "overall_status": r.overall_status,
            "today_overview": r.today_overview,
        }
        for r in rows
    ]


async def _fetch_today_materials(
    db_session_factory: async_sessionmaker[AsyncSession],
    owned_session_ids: frozenset[uuid.UUID],  # 调用方保证非空
    day_start: datetime,
    day_end: datetime,
) -> tuple[list[_TodayRollingSummaryItem], list[_CrisisMarkerItem]]:
    """查今日对话材料:rolling_summaries + crisis 标记。

    前置条件:owned_session_ids 非空(由 load_context 守门)。

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

### 2.3 删除项

- `load_context` 内联 `parts: list[str] = [...]` 整个块(原 graph.py:151-228)
- 节点体内 `from app.domain.audit.models import AuditRecord, RollingSummary` inline import(原 graph.py:167)
- `load_context` 注释中"1. 从 context 取 recent_reports_overview"等说明,改为新结构说明

---

## 模块 3:`context_schema.py` — 字段移除

`ExpertContextSchema.recent_reports_overview: list[dict]` 移除。

```diff
- recent_reports_overview: list[dict]  # 近 N 天历史报告摘要
```

docstring 同步清理(原"建图前查询,load_context 嵌入 prompt"段)。

---

## 模块 4:`worker.py` — 删除 `_get_recent_reports` + 调用点

- 删除 `_get_recent_reports` 函数定义(原 worker.py:116-156)
- 删除 `_report_for_child` 内的 5 行调用块(原 worker.py:286-291):
  ```python
  # e. recent_reports_overview
  recent_reports = await _get_recent_reports(
      child_db, child_user_id_val, report_date,
  )
  ```
- `ExpertContextSchema(...)` 构造调用删除 `recent_reports_overview=recent_reports` 行(原 worker.py:302)
- `import uuid` / `import date` 等若不再使用,按需清理

---

## 模块 5:测试更新

### 5.1 新增 `tests/expert/test_prompts.py`

5 个最小覆盖用例(无快照):

| 用例 | 输入 | 期望 |
|---|---|---|
| `test_empty_input` | 4 个参数全空 | 只含 `报告日期: {iso}` 一行 |
| `test_history_overview_only` | 只填 `recent_reports_overview` | 渲染 `## 近期历史报告概览` 段,日期头 + 无今日段 |
| `test_today_materials_only` | 只填 `today_rolling_summaries` | 渲染 `## 今日对话材料` 段(含 session 块 + notes),无危机段 |
| `test_crisis_markers` | 同时填今日材料 + crisis | 渲染 `## 危机标记` 段在今日材料内 |
| `test_full_data` | 4 段全填 | 4 段按固定顺序全渲染,日期头在最前 |

`date` / `DailyStatus` 字段构造用真实类型(`datetime.date(2026, 6, 25)` / `DailyStatus.alert`),验证 `.isoformat()` / `.value` 格式化生效。

### 5.2 修改现有测试文件

3 个文件需要从 `ExpertContextSchema` 构造中删除 `recent_reports_overview=...` 参数:

- `tests/integration/expert/test_tool_error_handling.py:57` — 删 `recent_reports_overview=[]`
- `tests/expert/test_tools.py:38` — 删 `recent_reports_overview=[]`
- `tests/expert/test_graph.py:125` — 删 `recent_reports_overview=[]`
- `tests/expert/test_graph.py:223` — 删 `_make_fake_runtime(recent_reports_overview=recent)` 调用

`test_graph.py` 中测试"近期历史报告概览"渲染逻辑的相关测试,需重写为 mock `_fetch_recent_reports` 或直接测试 `build_expert_first_human_message` 的输入输出(后者优先,更单元化)。

---

## 边界情况

| 场景 | 行为 |
|---|---|
| `recent_reports_overview=[]` | 跳过 `## 近期历史报告概览` 段(无空段占位) |
| `today_rolling_summaries=[]` 且 `crisis_markers` 非空 | 仅渲染 `## 危机标记` 段(本日无对话但审计有危机标记)。但生产路径下 `owned_session_ids` 非空 → 实际不会出现"无今日材料却有 crisis" 的情况,代码不做额外防御 |
| `today_rolling_summaries` 单个 session `turn_summaries=[]` 且 `session_notes=""` | 该 session 块不渲染任何东西(无 `### Session` 标题、无 notes),但 `## 今日对话材料` 总标题仍渲染 |
| `entry["turn_number"]` 缺字段 | `KeyError`,节点异常上抛 → arq worker `return_exceptions=True` 兜底记 error log。生产数据契约保证 `turn_number` 必在,缺字段即数据污染 |
| `owned_session_ids` 为空(生产不应发生) | `load_context` 记 error 日志,短路不开 DB session,首帧 human 仅含日期头 |
| 多次 `_fetch_today_materials` 调用同一 session | DB 内 `IN (...)` 正常处理;无重复行问题(SQLAlchemy ORM 自动去重) |

---

## 迁移清单(commit 顺序建议)

1. `app/domain/expert/prompts.py` — 新增 TypedDict + `build_expert_first_human_message`(独立可测)
2. `tests/expert/test_prompts.py` — 新增 5 用例(验证新 builder)
3. `app/domain/expert/graph.py` — 重构 `load_context` + 增 2 个 helper(调用新 builder)
4. `app/domain/expert/context_schema.py` — 删 `recent_reports_overview` 字段
5. `app/domain/expert/worker.py` — 删 `_get_recent_reports` + 调用点 + ctx 构造参数
6. 3 个测试文件 — 删 `recent_reports_overview` fixture 参数
7. `docker compose exec api ruff check` / `ruff format` / `basedpyright` — 三道闸通过
8. 跑 `tests/expert/` + `tests/integration/expert/` 全绿

每步独立 commit,便于回滚。

---

## 不在本次范围

- `expert/repository.py:231` 同款 `entry.get("turn", entry.get("turn_number"))` 死代码 → 留作 follow-up
- `expert/graph.py` 220+ 行的图拓扑重构 → 留作后续 PR
- `ExpertReportSchema` 6 段内容的具体措辞调整 → 由作者后续在 `prompts.py` 内手工微调
