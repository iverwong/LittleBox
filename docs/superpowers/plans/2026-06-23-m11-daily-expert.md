# M11 · 日终专家 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现日终专家 agentic graph，离线生成面向家长的每日教育观察报告。

**Architecture:** 复用 audit 域的 LangGraph agentic 拓扑（load_context → llm_call ↔ tools → write_results），工具全部只读 DB 查询。ARQ cron 于 04:05 触发，asyncio.gather + Semaphore(10) 并发遍历孩子。

**Tech Stack:** LangGraph StateGraph + ChatDeepSeek + SQLAlchemy async + ARQ cron + PostgreSQL JSONB + Pydantic v2

**Spec:** `docs/superpowers/specs/2026-06-23-m11-daily-expert-design.md`

## Global Constraints

- Python 3.14 PEP 758：`except` 支持不带括号多异常类型
- `messages.role`：DB 存 `human` / `ai`
- HTTPException 状态码用 `status.HTTP_xxx_xxx` 常量
- `core/*` 零业务依赖；`api/*` 只 import `core/*` + `domain/*/usecase`
- 测试隔离纪律：必须走 conftest fixture
- 所有 I/O 走 async
- 提交前：`docker compose exec api ruff format && ruff check && basedpyright`
- 运行环境：Docker Compose，所有命令通过 `docker compose exec api ...`

---

### Task 1：Worker 重构 — `app/worker.py` + 涟漪更新

**文件：**
- 创建：`backend/app/worker.py`
- 修改：`backend/app/domain/audit/worker.py` — 删除 WORKER_SETTINGS/生命周期钩子，仅保留 `run_audit` + `MAX_TRIES`
- 修改：`backend/docker-compose.yml` — `audit_worker` → `worker`，command 切到 `app.worker.WORKER_SETTINGS`
- 修改：`backend/app/domain/chat/usecase.py` — `AUDIT_JOB_NAME = "app.worker.run_audit"`
- 修改：`backend/tests/integration/conftest.py` — worker fixture `functions=["app.worker.run_audit"]`
- 修改：`backend/tests/integration/test_smoke.py` — 同上
- 修改：`backend/tests/integration/chat/test_contract_audit_job_name.py` — import `app.worker`
- 修改：`backend/tests/audit/test_worker.py` — import `app.worker.MAX_TRIES`
- 修改：`backend/tests/audit/test_e2e.py` — import 同步

**`app/worker.py` 核心结构：**
- `on_startup`：`build_runtime(settings)` → `ctx["resources"]`
- `on_shutdown`：`teardown_runtime(ctx["resources"])`
- `WORKER_SETTINGS`：`functions=["app.worker.run_audit", "app.domain.expert.worker.run_daily_reports"]`，`cron_jobs=[{hour: settings.expert_cron_hour, minute: settings.expert_cron_minute, coroutine: "app.domain.expert.worker.run_daily_reports"}]`，`job_timeout=3600`
- `run_audit`：`from app.domain.audit.worker import run_audit` 重新导出

- [ ] **Step 1：创建 `backend/app/worker.py`**（代码见上文结构）
- [ ] **Step 2：精简 `backend/app/domain/audit/worker.py`**（删除 WORKER_SETTINGS / 生命周期钩子，保留 `run_audit` + `MAX_TRIES=3`）
- [ ] **Step 3：更新 `backend/docker-compose.yml`**（service 改名 + command/healthcheck 路径）
- [ ] **Step 4：更新 6 个涟漪文件的 import / 字符串路径**
- [ ] **Step 5：运行既有测试** `docker compose exec api pytest tests/audit/ tests/integration/chat/test_contract_audit_job_name.py -v`
- [ ] **Step 6：ruff + basedpyright** `docker compose exec api ruff format && ruff check && basedpyright`
- [ ] **Step 7：Commit**

---

### Task 2：Role.EXPERT + 配置 + 对齐

**文件：**
- 修改：`backend/app/core/llm_topology.py` — `Role.EXPERT = "expert"`，`ROLES[Role.EXPERT]`（deepseek → bailian，thinking=ON，effort=MAX，retry=3）
- 修改：`backend/app/core/llm.py` — `build_expert_llm(settings, http_async_client=None)` 工厂函数
- 修改：`backend/app/core/config.py` — `expert_cron_hour: int = 4`，`expert_cron_minute: int = 5`，`expert_max_concurrent_children: int = 10`，`expert_token_budget: int = 100_000`
- 修改：`backend/app/domain/chat/session_policy.py` — `DAILY_SUMMARY_TRIGGER_HOUR = 5` → `= 4`

- [ ] **Step 1：`core/llm_topology.py`** — `Role` enum + ROLES 追加 `Role.EXPERT`
- [ ] **Step 2：`core/llm.py`** — `build_expert_llm()`（调用 `_build_role_llm(Role.EXPERT, ...)`）
- [ ] **Step 3：`core/config.py`** — 追加 4 个配置字段
- [ ] **Step 4：`chat/session_policy.py`** — `DAILY_SUMMARY_TRIGGER_HOUR = 4`
- [ ] **Step 5：验证** — `docker compose exec api python -c "from app.core.llm import build_expert_llm; print('OK')"`
- [ ] **Step 6：ruff + basedpyright**
- [ ] **Step 7：Commit**

---

### Task 3：Expert 契约层 — schemas + context_schema + prompts

**文件：** 全新建
- `backend/app/domain/expert/schemas.py`
- `backend/app/domain/expert/context_schema.py`
- `backend/app/domain/expert/prompts.py`

**schemas.py** — `SearchHistoryInput`（keywords list[1-8,≥2chars] + start/end date + limit 1-50 + context_chars 0-300 + sources）、`FetchByRefInput`（ref str + context_turns 0-3）、`ExpertReportSchema`（6 段 + overall_status + degraded bool）

**context_schema.py** — `ExpertContextSchema` frozen dataclass：child_user_id、owned_session_ids (frozenset[uuid])、report_date、dimension_summary、recent_reports_overview、crisis_detected_today、max_output_attempts、token_budget、child_profile、settings/db_session_factory/shared_http_client

**prompts.py** — `build_expert_system_prompt(max_output_attempts)` → SystemMessage，内容含身份/材料说明/6段输出格式/工作流/纪律

- [ ] **Step 1：创建 `schemas.py`**
- [ ] **Step 2：创建 `context_schema.py`**
- [ ] **Step 3：创建 `prompts.py`**
- [ ] **Step 4：pydantic 校验** — `docker compose exec api python -c "from app.domain.expert.schemas import SearchHistoryInput, ExpertReportSchema; ..."`
- [ ] **Step 5：ruff + basedpyright**
- [ ] **Step 6：Commit**

---

### Task 4：Expert Repository 层

**文件：** 新建 `backend/app/domain/expert/repository.py`

7 个函数，全部只读：
- `search_turn_summaries(db, child_user_id, keywords, start, end, limit, context_chars)` — JSONB array elements + ILIKE ANY
- `search_session_notes(db, child_user_id, keywords, start, end, limit, context_chars)` — TEXT ILIKE ANY + 开窗
- `search_crisis_topics(db, child_user_id, keywords, start, end, limit)` — audit_records.crisis_topic
- `search_daily_reports(db, child_user_id, keywords, start_date, end_date, limit, context_chars, exclude_report_date)` — 排除预填窗口
- `fetch_turn(db, session_id, turn_number, context_turns)` — turn_summary + human/ai 原文 + crisis 标记
- `fetch_notes(db, session_id)` — session_notes 全文 + 元信息
- `fetch_report(db, report_id)` — daily_report 完整 content

Helper：`_extract_snippet(text, keywords, context_chars)` — 长源按窗口截断

- [ ] **Step 1：创建 `repository.py`**
- [ ] **Step 2：验证 import** — `docker compose exec api python -c "from app.domain.expert.repository import *; print('OK')"`
- [ ] **Step 3：Commit**

---

### Task 5：Expert Tools 编排层

**文件：** 新建 `backend/app/domain/expert/tools.py`

- `_search_history(args, runtime, tool_call_id)` — pydantic 校验 SearchHistoryInput → 日期窗口校验（end < report_date；span ≤ 90 日）→ 扇出调 repository → 排序截断 → ToolMessage(JSON)
- `_fetch_by_ref(args, runtime, tool_call_id)` — pydantic 校验 → 正则解析 ref 格式 → sid in owned_session_ids 校验 → 调 repository → ToolMessage(bundle JSON)
- `EXPERT_TOOL_HANDLERS = {"SearchHistoryInput": _search_history, "FetchByRefInput": _fetch_by_ref}`

Ref 正则：`^(turn):([0-9a-fA-F-]{36})#(\d+)$|^(notes):([0-9a-fA-F-]{36})$|^(report):([0-9a-fA-F-]{36})$`

- [ ] **Step 1：创建 `tools.py`**
- [ ] **Step 2：验证** — `docker compose exec api python -c "from app.domain.expert.tools import EXPERT_TOOL_HANDLERS; print('OK')"`
- [ ] **Step 3：Commit**

---

### Task 6：Expert LLM 装配

**文件：** 新建 `backend/app/domain/expert/llm.py`

`build_expert_llm(settings, http_async_client=None)` — `build_role_primary(Role.EXPERT)` + `build_role_fallback(Role.EXPERT)` → 各 `.bind_tools([SearchHistoryInput, FetchByRefInput, ExpertReportSchema])` → `wrap_resilience(primary_bound, fallback_bound, retry_attempts=ROLES[Role.EXPERT].retry_attempts)`

- [ ] **Step 1：创建 `llm.py`**
- [ ] **Step 2：验证 import**
- [ ] **Step 3：Commit**

---

### Task 7：Expert Graph（agentic 核心）

**文件：** 新建 `backend/app/domain/expert/graph.py`

**State (`ExpertGraphState`):** messages (add_messages)、output_attempts、total_output_tokens、structured_output

**4 节点：**

1. **`load_context`** — 从 context 取 recent_reports_overview + 查询今日时间线（跨所有 session 的 turn_summaries + crisis 标记 + session_notes）→ 嵌入 HumanMessage 首帧
2. **`expert_llm_call`** — 调 `build_expert_llm`，累加 `total_output_tokens`（从 `response_metadata.token_usage.output_tokens`）；无 tool_calls 追问一轮，仍无 → 由 expert_tools 兜底
3. **`expert_tools`** — 薄派发：按 tool name → handler / ExpertReportSchema pydantic 校验；output_attempts 仅计数输出工具；token budget 超限注入 HumanMessage "立即停止收集" + 后续 search/fetch → error ToolMessage；output_attempts >= max_output_attempts → degraded
4. **`write_results`** — 双层 overall_status 兜底（crisis_detected_today → alert；degraded → attention）+ 调 `usecase.write_expert_results()`

**条件路由：** `route_after_tools` — structured_output 非空 → write_results，否则 → expert_llm_call

**build_expert_graph()** — 无参工厂，context_schema=ExpertContextSchema

- [ ] **Step 1：创建 `graph.py`**（包含上述 4 节点 + 路由 + 工厂）
- [ ] **Step 2：验证 import** — `docker compose exec api python -c "from app.domain.expert.graph import build_expert_graph; print('OK')"`
- [ ] **Step 3：ruff + basedpyright**
- [ ] **Step 4：Commit**

---

### Task 8：Expert Usecase（写入层）

**文件：** 新建 `backend/app/domain/expert/usecase.py`

`write_expert_results(db, child_user_id, report_date, output, dimension_summary)` — upsert daily_reports：

```sql
INSERT INTO daily_reports (child_user_id, report_date, overall_status, dimension_summary, content, degraded)
VALUES (...)
ON CONFLICT (child_user_id, report_date) DO UPDATE SET
    overall_status = EXCLUDED.overall_status,
    dimension_summary = EXCLUDED.dimension_summary,
    content = EXCLUDED.content,
    degraded = EXCLUDED.degraded;
```

- [ ] **Step 1：创建 `usecase.py`**
- [ ] **Step 2：验证**
- [ ] **Step 3：Commit**

---

### Task 9：Expert Worker + Runtime 集成

**文件：**
- 创建：`backend/app/domain/expert/worker.py`
- 修改：`backend/app/core/runtime.py` — `RuntimeResources` 增 `expert_graph`，`build_runtime` 惰性编译

**`worker.py` — `run_daily_reports(ctx)`**：
1. `report_date = logical_day(now, boundary_hour=4) - 1day`
2. 查活跃孩子（JOIN ChildProfile）
3. 每个孩子：查 `owned_session_ids` + ChildProfileSnapshot + `_check_crisis_today` + `_aggregate_dimensions` + `_get_recent_reports`
4. 构造 `ExpertContextSchema` + `ExpertGraphState`
5. `asyncio.gather` + `Semaphore(settings.expert_max_concurrent_children)` 并发，per-child 捕获异常不传播

**`runtime.py`** — `build_runtime` 中追加：
```python
from app.domain.expert.graph import build_expert_graph
expert_graph = build_expert_graph()
```
`RuntimeResources` dataclass 新增 `expert_graph: CompiledStateGraph`

- [ ] **Step 1：创建 `worker.py`**（含 helper：`_check_crisis_today` / `_aggregate_dimensions` / `_get_recent_reports`）
- [ ] **Step 2：更新 `core/runtime.py`** — `RuntimeResources` + `build_runtime`
- [ ] **Step 3：验证 graph 编译** — `docker compose exec api python -c "from app.core.runtime import build_runtime; ..."`
- [ ] **Step 4：ruff + basedpyright**
- [ ] **Step 5：Commit**

---

### Task 10：清理 + 迁移（需求 §九）

**文件：**
- 修改：`backend/app/domain/expert/models.py` — 注释修正
- 新建：`backend/alembic/versions/<hash>_m11_daily_reports_unique_upsert.py`
- 修改：`backend/app/domain/notifications/notify_stub.py` — 新增 `send_daily_summary` stub
- 修改：PG enum `NotificationType` 清残留 `redline`（仅 DB）

**models.py 注释变更：**
- `dimension_summary`: "LLM 量化锚点" → "UI / 跨日对比"
- `high_turns` 字段/注释 → `high_ratio`（占比口径）
- `overall_status`: "LLM 综合判断" → "LLM 判断 + 危机态代码地板"
- `report_date`: 明确 `boundary_hour=4`

**迁移：**
```python
def upgrade():
    op.drop_index('idx_reports_child', table_name='daily_reports')
    op.create_index('idx_reports_child', 'daily_reports', ['child_user_id', 'report_date'], unique=True)
```

**notify_stub.py：**
```python
def send_daily_summary(child_user_id, report_date, overall_status):
    logger.info("notify.stub.daily_summary child=%s date=%s status=%s", child_user_id, report_date, overall_status)
```

- [ ] **Step 1：`expert/models.py`** 注释修正
- [ ] **Step 2：`notify_stub.py`** 新增 `send_daily_summary` stub
- [ ] **Step 3：生成 alembic 迁移** — `docker compose exec api alembic revision -m "m11_daily_reports_unique_upsert"`，填充 upgrade/downgrade
- [ ] **Step 4：运行迁移** — `docker compose exec api alembic upgrade head`
- [ ] **Step 5：DB 清理** — 检查 PG enum `NotificationType` 中 `redline` 残留并清理
- [ ] **Step 6：ruff + basedpyright**
- [ ] **Step 7：Commit**

---

### Task 11：测试

**文件：** 新建 `backend/tests/expert/`

- `test_schemas.py` — ExpertReportSchema / SearchHistoryInput / FetchByRefInput 校验通过/失败路径
- `test_repository.py` — 7 个查询函数的 SELECT 正确性（concurrent_db_sessions）
- `test_tools.py` — search_history / fetch_by_ref handler 入参校验 + 边界 case
- `test_graph.py` — 4 节点单元测试（fake LLM + mock DB session），验证 load_context / expert_llm_call / expert_tools / write_results 各个路径
- `test_worker.py` — run_daily_reports happy/failure 路径

- [ ] **Step 1：`test_schemas.py`** — 写 Schema 校验测试，跑通
- [ ] **Step 2：`test_repository.py`** — 写只读查询测试，跑通
- [ ] **Step 3：`test_tools.py`** — 写 handler 测试，跑通
- [ ] **Step 4：`test_graph.py`** — fake LLM + mock DB 测试图各节点
- [ ] **Step 5：`test_worker.py`** — 测试并发 + 失败隔离
- [ ] **Step 6：全量回归** — `docker compose exec api pytest tests/ -v`
- [ ] **Step 7：ruff + basedpyright**
- [ ] **Step 8：Commit**

---

### Task 12：最终验证

- [ ] **Step 1：全量测试** `docker compose exec api pytest tests/ -v`
- [ ] **Step 2：ruff format + ruff check + basedpyright**
- [ ] **Step 3：docker-compose 启动** `docker compose up -d`，确认 worker service 正常
- [ ] **Step 4：Commit**
