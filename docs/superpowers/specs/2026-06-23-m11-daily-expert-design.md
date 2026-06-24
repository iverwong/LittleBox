# M11 · 日终专家 — 执行方案

## 背景

本设计文档基于 `docs/M11 · 日终专家 — 关键决策对齐.md` 中已与 Iver 对齐的 11 节决策（✅ 标注），结合实际代码探索结果产出的最终执行方案。

## 执行顺序

1. Worker 重构 + Role.EXPERT（基础设施先行）
2. Expert 域 schemas + prompts + context_schema（契约层）
3. Expert repository + tools（数据源层）
4. Expert graph + llm + usecase（agentic 核心）
5. Expert worker + cron（触发层）
6. 清理 + 迁移（收尾）
7. 测试 + 涟漪更新

---

## 模块 1：Worker 重构（需求 §三）

### 1.1 新建 `backend/app/worker.py`（与 main.py 同级）

聚合 `WORKER_SETTINGS`：`functions=[run_audit, run_daily_reports]` + `cron_jobs` + 生命周期钩子。

- `run_audit` 保留在 `audit/worker.py`——只做 job function，不再定义 `WORKER_SETTINGS`
- 生命周期钩子 `on_startup` / `on_shutdown`：调用 `build_runtime` / `teardown_runtime`，把 `RuntimeResources`（含 `audit_graph` + `expert_graph`）写入 `ctx["resources"]`

### 1.2 `docker-compose.yml`

- service 名 `audit_worker` → `worker`
- command：`arq app.worker.WORKER_SETTINGS`
- healthcheck：`test: ["CMD", "arq", "app.worker.WORKER_SETTINGS", "--check"]`

### 1.3 涟漪更新

| 文件 | 变更 |
|------|------|
| `chat/usecase.py` | `AUDIT_JOB_NAME` = `"app.worker.run_audit"` |
| `tests/integration/conftest.py` | worker fixture 内字符串路径同步 |
| `tests/integration/test_smoke.py` | 字符串路径同步 |
| `tests/integration/chat/test_contract_audit_job_name.py` | import 路径切到 `app.worker` |
| `tests/audit/test_worker.py` | import 路径切到 `app.worker` |
| `tests/audit/test_e2e.py` | import 路径切到 `app.worker` |

---

## 模块 2：Role.EXPERT + LLM 装配（需求 §五）

### 2.1 `core/llm_topology.py`

```python
class Role(StrEnum):
    MAIN = "main"
    AUDIT = "audit"
    COMPRESSION = "compression"
    EXPERT = "expert"  # 新增
```

`ROLES[Role.EXPERT]`：与 MAIN/AUDIT 同配置——deepseek v4 flash，thinking ON，reasoning_effort=MAX，bailian fallback，retry_attempts=3。

### 2.2 `core/llm.py`

新增 `build_expert_llm(settings, http_async_client=...)`：通过 `_build_role_llm(Role.EXPERT, ...)` 拿裸实例，调用方自行 bind_tools + wrap_resilience（与 audit 模式一致）。

### 2.3 `core/config.py`

```python
expert_cron_hour: int = 4
expert_cron_minute: int = 5
expert_max_concurrent_children: int = 10
expert_token_budget: int = 100_000
```

### 2.4 `chat/session_policy.py`

`DAILY_SUMMARY_TRIGGER_HOUR = 5` → `= 4`，与需求 §二对齐。

---

## 模块 3：Expert Agentic Graph（需求 §五 + §八）

拓扑：`load_context → expert_llm_call ↔ expert_tools → write_results`

与 audit 共享设计模式但更简单：工具只做只读 DB 查询（无 ReplaceInNotes 状态改写），无跨节点 sid/turn 传递。

### 3.1 `expert/context_schema.py`

`ExpertContextSchema` frozen dataclass：

| 字段 | 说明 |
|------|------|
| `child_user_id` | 孩子 UUID（DI 注入，不进工具参数） |
| `owned_session_ids` | `frozenset[uuid.UUID]`，该孩子所有 session ID 白名单（建图前一次性查出） |
| `report_date` | 刚结束的逻辑日（`logical_day(now, boundary_hour=4) - 1day`） |
| `dimension_summary` | `dict` 代码预聚合的 6 维 peak/mean/high_ratio（不喂 LLM，图内 write_results 直写 DB） |
| `recent_reports_overview` | `list[dict]`，近 N 天历史报告摘要 `[{report_date, overall_status, today_overview}]`（建图前查询，`load_context` 嵌入 prompt） |
| `crisis_detected_today` | `bool`，当日逻辑窗口内是否有任一 `crisis_detected=True`（worker 层预查，供 `overall_status` 地板判定） |
| `max_output_attempts` | ExpertReportSchema 调用上限，默认 3（仅计数输出工具调用，search/fetch 不限） |
| `token_budget` | 资料收集 token 预算，默认 100_000（累计 LLM 输出 token 超限时注入强制交卷 HumanMessage） |
| `child_profile` | `ChildProfileSnapshot`（用于 prompt） |
| `settings` / `db_session_factory` / `shared_http_client` | 资源注入 |

### 3.2 `expert/schemas.py`

**`ExpertReportSchema`**（bound tool，对应 audit 的 `AuditOutputSchema`）：

```python
class ExpertReportSchema(BaseModel):
    overall_status: DailyStatus        # LLM 判断；crisis_detected=True → 代码地板强制 alert
    degraded: bool = False             # True 时表示降级产物（max_iter 超限 / LLM 未调工具）
    today_overview: str                # 1. 今日概览
    what_was_discussed: str            # 2. 聊了什么
    emotion_changes: str               # 3. 情绪变化
    noteworthy: str                    # 4. 值得关注
    suggestions: str                   # 5. 具体建议
    anomaly_periods: str               # 6. 异常时段标注
```

**工具入参 schema**：

- `SearchHistoryInput`：`keywords` (list[str], 1-8, ≥2 chars)、`start_date`/`end_date` (可选, ISO date)、`limit` (默认 15, clamp [1,50])、`context_chars` (默认 100, clamp [0,300])、`sources` (可选, 4 类枚举子集)
- `FetchByRefInput`：`ref` (str)、`context_turns` (默认 0, clamp [0,3])

### 3.3 `expert/prompts.py`

System prompt 内容要点：
- 身份：日终教育观察专家，面向家长
- 材料说明：时间线、session_notes、危机标记，及如何使用历史报告
- 输出格式：调用 `ExpertReportSchema` 工具，6 段均需填写
- 工作流：先看预填历史报告 → 再看今日材料 → 必要时 search_history 回溯 → fetch_by_ref 核实原文 → 给出报告
- 安全纪律：没查到就是"近期首次出现"、不臆造连续性；`owned_session_ids` 在 DI 层隐式校验

### 3.4 `expert/llm.py`

```python
def build_expert_llm(settings, http_async_client=None) -> Runnable:
    primary = build_role_primary(Role.EXPERT, settings, http_async_client=http_async_client)
    fallback = build_role_fallback(Role.EXPERT, settings, http_async_client=http_async_client)
    tools = [SearchHistoryInput, FetchByRefInput, ExpertReportSchema]
    primary_bound = primary.bind_tools(tools)
    fallback_bound = fallback.bind_tools(tools)
    return wrap_resilience(primary_bound, fallback_bound, retry_attempts=ROLES[Role.EXPERT].retry_attempts)
```

### 3.5 `expert/graph.py`

4 节点 + 1 条件路由：

```
START → load_context → expert_llm_call → expert_tools
                           ↑                 │
                           └─── loop ────────┤ (structured_output 为空)
                                             └──→ write_results → END
                                             (structured_output 非空)
```

**State (`ExpertGraphState`)**：

| 字段 | 说明 |
|------|------|
| `messages` | `add_messages` reducer |
| `output_attempts` | ExpertReportSchema 调用计数（仅输出工具，search/fetch 不计数） |
| `total_output_tokens` | 累计 LLM 输出 token 数（从 AIMessage response_metadata 累加，用于 token budget） |
| `structured_output` | `ExpertReportSchema \| None` |

**`load_context`**：从 context 取 `recent_reports_overview`（worker 层已查好）→ 嵌入首帧 prompt；查询今日时间线材料（turn_summaries + session_notes + crisis 标记）→ 构造 HumanMessage。

**`expert_llm_call`**：调 `build_expert_llm`，无 tool_calls 时追问一轮（HumanMessage 要求调工具），仍无 → 降级 `degraded=True`。节点返回前从 AIMessage.response_metadata 累加 `total_output_tokens`。

**`expert_tools`**（薄派发节点）：
- 按 tool name 路由：
  - `SearchHistoryInput` / `FetchByRefInput` → 调 handler、返回 ToolMessage，**不增加** `output_attempts`
  - `ExpertReportSchema` → pydantic 校验。成功 → 设 `structured_output` + `output_attempts += 1`；失败 → error ToolMessage + `output_attempts += 1`
- **Token budget 检查**：若 `total_output_tokens` 超过 `token_budget`（100K），先注入一条 HumanMessage 强制交卷（"立即停止收集，调用 ExpertReportSchema 给出最终报告"），此后的 search/fetch 工具调用返回 error ToolMessage（"token quota exhausted"），仅接收 ExpertReportSchema
- `output_attempts >= max_output_attempts`（3 次交卷机会用完）：降级 `degraded=True`
- 异常统一 → error ToolMessage

**`write_results`**：从 state 取 `structured_output`，执行 `overall_status` 双层兜底（crisis 地板 + degraded 保守默认），调 `usecase.write_expert_results()` upsert 到 `daily_reports`。

### 3.6 `expert/tools.py`

两个 handler（编排层）：

**`search_history`**：
1. 解析 `SearchHistoryInput`（pydantic 校验）
2. 从 Runtime context 取 `child_user_id` + `owned_session_ids`
3. 按 `sources` 扇出调用 repository 层的查询函数
4. 合并结果、`occurred_at` DESC 排序、LIMIT 截断
5. 返回 ToolMessage（JSON）

**`fetch_by_ref`**：
1. 解析 ref 字符串为 `(kind, sid, extra)` 
2. 从 Runtime context 取 `child_user_id` + `owned_session_ids`
3. 内存校验：`sid in owned_session_ids`
4. 按 kind 调 repository 层查询
5. 返回 ToolMessage（bundle JSON）

### 3.7 `expert/repository.py`

只读查询函数，每个数据源一个：

| 函数 | 查什么 | 返回 |
|------|--------|------|
| `search_turn_summaries(child_user_id, keywords, date_range, limit, context_chars)` | `rollings_summaries.turn_summaries` JSONB 元素 + `sessions` JOIN | list[Hit] |
| `search_session_notes(child_user_id, keywords, date_range, limit, context_chars)` | `rollings_summaries.session_notes` TEXT + `sessions` JOIN | list[Hit] |
| `search_crisis_topics(child_user_id, keywords, date_range, limit)` | `audit_records.crisis_topic` + `sessions` JOIN | list[Hit] |
| `search_daily_reports(child_user_id, keywords, date_range, limit, context_chars, exclude_window)` | `daily_reports.content` TEXT | list[Hit] (排除预填窗口) |
| `fetch_turn(session_id, turn_number)` | 该轮 turn_summary + human/ai 原文 (from messages) + crisis 标记 | TurnBundle |
| `fetch_notes(session_id)` | `rollings_summaries.session_notes` 全文 + 元信息 | NotesBundle |
| `fetch_report(report_id)` | `daily_reports` 完整 content | ReportBundle |

**查询骨架**（以 `search_turn_summaries` 为例）：

```sql
SELECT rs.turn_summaries, s.id AS session_id, m.created_at
FROM rolling_summaries rs
JOIN sessions s ON s.id = rs.session_id
JOIN messages m ON m.session_id = s.id AND m.turn_number = ... 
WHERE s.child_user_id = :child_user_id
  AND m.created_at BETWEEN :start AND :end
  AND ... -- jsonb_array_elements + ILIKE ANY(:keywords)
ORDER BY m.created_at DESC
LIMIT :limit
```

### 3.8 `expert/usecase.py`

`write_expert_results(db, child_user_id, report_date, output, dimension_summary)`：

```sql
INSERT INTO daily_reports (child_user_id, report_date, overall_status, dimension_summary, content, degraded)
VALUES (...)
ON CONFLICT (child_user_id, report_date) DO UPDATE SET
    overall_status = EXCLUDED.overall_status,
    dimension_summary = EXCLUDED.dimension_summary,
    content = EXCLUDED.content,
    degraded = EXCLUDED.degraded;
```

两层 `overall_status` 兜底：
1. **图内 `write_results` 节点**：若 `crisis_detected_today=True`（从 context 读取），强制将 `structured_output.overall_status` 覆写为 `alert`
2. **图内降级路径**：若 `degraded=True`（LLM 未产出完整报告），`overall_status` 走保守默认——有危机 → `alert`，无危机 → `attention`

注：`dimension_summary` 由代码从 `audit_records.dimension_scores` 聚合（不喂 LLM），在 worker 层 `run_daily_reports` 建图前查询后注入 context，`write_results` 节点直接写入。

### 3.9 `expert/worker.py`

```python
async def run_daily_reports(ctx, ...) -> None:
    rr = ctx["resources"]
    report_date = logical_day(datetime.now(UTC), boundary_hour=4) - timedelta(days=1)

    # 遍历所有活跃孩子（有 ChildProfile 的才可生成报告）
    async with rr.db_session_factory() as db:
        children = await db.execute(
            select(User.id).join(ChildProfile).where(
                User.role == UserRole.child, User.is_active == True
            )
        )
        child_ids = [row[0] for row in children]

    sem = asyncio.Semaphore(settings.expert_max_concurrent_children)  # 默认 10
    
    async def _report_for_child(child_user_id):
        async with sem:
            async with rr.db_session_factory() as db:
                owned_session_ids = frozenset(
                    s.id for s in await db.execute(
                        select(Session.id).where(Session.child_user_id == child_user_id)
                    )
                )

                # 查 child_profile 构造 snapshot
                profile = await db.scalar(
                    select(ChildProfile).where(ChildProfile.child_user_id == child_user_id)
                )
                snapshot = ChildProfileSnapshot(...)

                # 预查 crisis 标记（供 overall_status 地板）
                crisis_detected_today = await _check_crisis_today(db, child_user_id, report_date)

                # 从 audit_records 聚合 dimension_summary（不喂 LLM）
                dim_summary = await _aggregate_dimensions(db, child_user_id, report_date)

                # 查近 7 天历史报告摘要
                recent = await _get_recent_reports(db, child_user_id, report_date, days=7)

            expert_ctx = ExpertContextSchema(
                child_user_id=child_user_id,
                owned_session_ids=owned_session_ids,
                report_date=report_date,
                dimension_summary=dim_summary,
                recent_reports_overview=recent,
                crisis_detected_today=crisis_detected_today,
                max_output_attempts=3,
                token_budget=100_000,
                child_profile=snapshot,
                settings=rr.settings,
                db_session_factory=rr.db_session_factory,
                shared_http_client=rr.shared_http_client,
            )

            state = ExpertGraphState(
                messages=[],
                output_attempts=0,
                total_output_tokens=0,
                structured_output=None,
            )
            await rr.expert_graph.ainvoke(state, context=expert_ctx)

    # gather: per-child 失败不波及同批次其余 child
    results = await asyncio.gather(
        *[_report_for_child(c) for c in child_ids],
        return_exceptions=True,
    )
    for child_id, result in zip(child_ids, results):
        if isinstance(result, Exception):
            logger.error("expert.child_failed child=%s err=%s", child_id, result)
```

**并发量与规模估算**：
- `expert_max_concurrent_children = 10`
- 内测初期：100 家庭、≤200 孩子
- 每个孩子 2-5 分钟（含多轮 tool call）：200 / 10 × 3min ≈ 60 分钟
- 04:05 触发，5 点前收工，覆盖窗口充足
- ARQ `job_timeout` 从 60s → 3600s（1 小时），覆盖整批最坏情况
- 每条 LLM 回复后从 `response_metadata.token_usage` 读取输出 token，累加到 `total_output_tokens`

### 3.10 `overall_status` 判定逻辑（需求 §四）

**两层策略**：

1. **代码硬地板**（worker 层预查 `crisis_detected_today`，写入 context）：遍历当日逻辑窗口内所有 `audit_records`，若任一 `crisis_detected=True` → 代码层强制 `alert`，覆盖 LLM 输出的 `overall_status`
2. **LLM 判断**（图内）：无危机时由 LLM 综合 6 段内容判定 `stable` / `attention`

降级场景（`degraded=True`，由 3 次交卷耗尽或 LLM 持续不调输出工具触发）：
- `content` 填诊断信息，`today_overview` 等字段尽已收集材料写入
- `overall_status` 用代码地板（有 crisis → alert，无 → attention 作为保守默认）

### 3.11 Token 预算与强制交卷

**触发条件**：图内 `expert_tools` 节点在每次 LLM 回复后检查 `total_output_tokens`（从 `response_metadata.token_usage.output_tokens` 累加）。超过 `token_budget`（100K）时：

1. 若当前 reply 有 tool_calls 且全部是 search/fetch → 注入一条 HumanMessage：
   > "你已收集了大量材料。现在请立即停止检索，综合已有信息，调用 ExpertReportSchema 给出最终报告。"
2. 此后的 search/fetch 工具调用返回 error ToolMessage（"token quota exhausted, please submit your report now"）
3. LLM 仍有 3 次 ExpertReportSchema 交卷机会（`output_attempts` 不受 token budget 影响）
4. `job_timeout` 3600s 为最终兜底

---

## 模块 4：工具 Harness 细节（需求 §八）

### 4.1 ① 历史日终报告 → 预填，不做工具

`load_context` 节点查近 N 天（建议 7 天）`daily_reports`，格式化为 `(date, status, 概览)` 列表嵌入首帧 prompt。不占工具轮次。

### 4.2 ② `search_history` 细节

- **多词 OR 匹配**：`ILIKE ANY(ARRAY[:keywords])`，命中任一词即返
- **日期窗口**：`end_date` 默认 = `report_date - 1day`，`start_date` 默认 = `end_date - 30days`，`start ≤ end`、`end < report_date`、跨度 clamp ≤ 90 日
- **出处分类（4 类）**：`turn_summary` / `crisis_topic` / `session_notes` / `daily_report`
- **原始 message 不进检索**：噪声大
- **返回字段**：`ref` / `source` / `snippet` / `occurred_at` / `matched` / `locating`
- **`context_chars` 语义**：仅对长源（`session_notes` / `daily_report`）开窗，以匹配位置为中心取前 N + 匹配文本 + 后 N；短源（`turn_summary` / `crisis_topic`）整段返

### 4.3 ③ `fetch_by_ref` 细节

**引用格式**：
- `turn:{session_id}#{turn}`：返回 turn_summary + human/ai 原文 + crisis 标记（不含 dim_scores）
- `notes:{session_id}`：返回 session_notes 全文 + 元信息
- `report:{report_id}`：返回完整 daily_report content

**`context_turns`**：仅 `turn:` 类生效，展开前后各 N 轮原文（clamp [0,3]）。

**安全**：`sid in owned_session_ids` 内存校验 + `child_user_id` 比对（均从 Runtime context 读取）。

### 4.4 安全地基

- `child_user_id` + `owned_session_ids` 走 `ExpertContextSchema` 注入，绝不进工具参数
- state 只留可变工作数据，不变量进 context
- 集合在单次 run 内稳定（04:05 后新建 session 属次日）

---

## 补充：维度评分趋势 API（前端消费者）

### 数据源

`audit_records.dimension_scores`（per-turn JSONB）已是 6 维时间序列的完整事实源。每行几十字节 × 每日最多数十轮，带索引命中毫秒级。

### API

```
GET /api/v1/children/{child_user_id}/dimension-trends?days=30
```

后端 JOIN `audit_records` + `sessions` 按 `child_user_id` 过滤 + `created_at` 日期范围，按日取 `MAX()` 或 `AVG()` 聚合返回：

```json
[
  {"date": "2026-06-22", "emotional": 7, "social": 3, "values": 2, "boundaries": 1, "academic": 4, "lifestyle": 2},
  ...
]
```

### 与 ExpertContextSchema 的关系

**不入 ExpertContextSchema**——数字不喂 LLM（需求 §六），LLM 不消费它。`dimension_summary`（聚合 peak/mean/high_ratio）在 context 里仅用于 `write_results` 节点直写 DB，不走 LLM 路径。

---

## 模块 5：清理清单（需求 §九）

| # | 文件 | 变更 |
|---|------|------|
| 1 | `expert/models.py` | `dimension_summary` 注释："LLM 量化锚点" → "UI / 跨日对比"；`high_turns` → `high_ratio`（占比口径）；`overall_status` 注释："LLM 综合判断" → "LLM 判断 + 危机态代码地板"；`report_date` 注释：明确 `boundary_hour=4` |
| 2 | 新增迁移 | `idx_reports_child` 改 UNIQUE `(child_user_id, report_date)` + `ON CONFLICT` upsert |
| 3 | PG enum | `NotificationType` 残留 `redline` 值清理（Python `core/enums.py` 已无此值，只清 DB） |
| 4 | `notify_stub.py` | 新增独立 `daily_summary` stub（`child_user_id` / `report_date` / `overall_status`），不复用 crisis stub |
| 5 | `core/runtime.py` | `RuntimeResources` 增 `expert_graph`；`build_runtime` 惰性 import 编译 |
| 6 | `core/config.py` | 增 `expert_cron_hour` / `expert_cron_minute` / `expert_max_concurrent_children` / `expert_token_budget` |
| 7 | 基线迁移 `1d8a14cc596f` | `dimension_summary` 注释 "7 维度" → "6 维" |
| 8 | 架构文档 | §十四"图外 commit" → "图内 commit"（audit 实际为图内 commit，文档描述有误） |

---

## 模块 6：测试

- `tests/expert/` — 新建目录
  - `test_schemas.py`：`ExpertReportSchema` / 工具 schema 校验
  - `test_repository.py`：只读查询函数正确性
  - `test_tools.py`：`search_history` / `fetch_by_ref` handler 的解析 + 错误路径
  - `test_graph.py`：4 节点单元测试（fake LLM + fake DB）
  - `test_worker.py`：`run_daily_reports` happy / failure 路径
- 既有测试涟漪更新（上述模块 1.3 列表）
