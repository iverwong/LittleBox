# M8 · 审查 Pipeline — 实施计划 (8/17)

<aside>
🛡️

**M8 · 审查 Pipeline — 实施计划（8/17）**

阶段 3 详细化定稿（2026-05-14）。范围：LangGraph 审查图 agentic loop + ARQ Worker + Redis `audit:\{sid\}` 三态信号管道 + session_notes 增量编辑工具 + ai_turn_counter 接入。M8 与 M7 并行实施；M7.5 / M7.6 顺延 M9 之后。

**分支**：`feat/m8-audit-pipeline`

</aside>

## 一、目标概述

按 [执行规划：17 个里程碑](https://www.notion.so/17-de81294334b947ef8d598245c73832ad?pvs=21) M8 范围，后端审查闭环全套落地：

- LangGraph 审查图三阶段（load_context → audit_llm_call ↔ audit_tools loop → write_results）
- ARQ Worker 独立 compose service，共享 image 仅 command 不同
- 审查 LLM = deepseek-v4-flash + thinking enabled + reasoning_effort=max；复用 M6 `_PROVIDER_REGISTRY` 工厂注册独立 `audit_provider` 配置
- structured output 8 字段：dimension_scores（7 维 0-10）+ crisis_detected + crisis_topic + redline_triggered + redline_detail + guidance + turn_summary
- session_notes 改 agentic tool 增量编辑（append_note + replace_in_notes，严格匹配不降级；每次调用返当前全文）；超出循环上限 5 次降级 append
- Redis `audit:\{sid\}` 单 key 三态（pending / ready / failed），TTL 24h，value JSON 携带 turn 用于主图正确性校验
- 主图 M6 三 stub 替换（load_audit_state / enqueue_audit / inject_guidance）；enqueue_audit 改 await 同步写 pending 占位
- sessions 表新增 `ai_turn_counter` 字段（M8 唯一 alembic revision），persist_ai_turn 同事务 +1
- write_results 单事务：audit_records INSERT + rolling_summaries upsert（SELECT FOR UPDATE + `WHERE last_turn < :turn` 防回退）

预估：**5 天**（对照路线图 M8 估算）。

## 二、不做什么（M8 范围外）

- 真 crisis / redline 干预 LLM 调用 → M9（call_crisis_llm / call_redline_llm 节点；M8 期 inject_guidance 节点保持透传，主图按信号路由但 crisis / redline 暂走 main，M9 时再切换）
- notify_sent 实际发送 → M11（接阿里云移动推送）
- 日终专家图 / daily_reports 生成 → M12
- M7.5 内测发布 / M7.6 热更新 → 顺延 M9 之后
- 子账号 LLM 日额软降级 → M7 前置，与 M8 主线无关
- session_notes 自动压缩（>8000 字符 LLM 自我压缩工具）→ M9/M10 评估；M8 期 session 长度有限不会触上限
- 审查任务跨 worker 并发 / 多机部署 → MVP 单 worker，scale-out 留 M14+
- alembic 新表 → 仅一次 ALTER TABLE 加 ai_turn_counter 列

## 三、前置条件

阶段 3 详细化启动门槛（已全部满足）：

- [x]  M6 全部 Step + M6-patch3 合并 main（load_audit_state / inject_guidance / enqueue_audit 三 stub 节点 + TODO(M8) 锚点就位）
- [x]  M6 LLM 抽象重构 + ChatDeepSeek 切换合并 main（`_PROVIDER_REGISTRY` 通用工厂可注册新 provider）
- [x]  audit_records / rolling_summaries ORM 已建表（M2，M8 无需新建表，仅一次 ALTER）
- [x]  `app/chat/state.py` MainDialogueState 含 `audit_state: dict` 字段及 M8 TODO 注释
- [x]  `app/chat/context.py` `build_context` 已留 `rolling_summaries.turn_summaries` 非空 fallthrough 注入路径（M8 期不主动消费，M9 接闭环）
- [x]  `app/schemas/__init__.py` 空文件，AuditDimensionScores / TurnSummaryEntry 等 Pydantic schema 由 M8 Step 2 新建
- [x]  M8 阶段 1 / 阶段 2 全部决策点收口（见 §四）
- [x]  [M6 · [sse.py](http://sse.py) dev compat 路径 child_profile=None 容忍（M7 Step 0 cleanup 收口）](https://www.notion.so/M6-sse-py-dev-compat-child_profile-None-M7-Step-0-cleanup-82914c6d01b74d12a84b8e2823b449f8?pvs=21) 归属修正为 M7 Step 0 cleanup 收口（2026-05-14 同步完成）
- [x]  [执行规划：17 个里程碑](https://www.notion.so/17-de81294334b947ef8d598245c73832ad?pvs=21) M7.5 / M7.6 顺延 M9 之后标注（2026-05-14 同步完成）

## 四、已锁决策回顾

阶段 1 + 阶段 2 跨多轮讨论锁定的全部决策点：

| # | 决策点 | 决议 |
| --- | --- | --- |
| D1 | ARQ Worker 部署形态 | 同 compose 独立 service，共享 image，两 service 不同 `command:`；不同进程隔离 OOM / 崩溃影响面 |
| D2 | 审查 LLM 选型 | 复用 deepseek，独立 `audit_provider`  • `audit_model=deepseek-v4-flash`  • `audit_reasoning_effort=max`  • `audit_thinking_enabled=true`（DeepSeek 文档：thinking 模式下 low/medium 被映射为 high，必须显式 max 才能拿最深推理） |
| D3 | Redis 信号管道形态 | 单 key `audit:\{sid\}` 三态 JSON（status: pending / ready / failed），value 携带 turn；TTL 24h |
| D4 | pending 占位时机 | enqueue_audit 改 await：先 SET pending 再 ARQ enqueue 再返主对话 SSE；放弃 fire-and-forget 模式（代价 5-10ms，换确定性） |
| D5 | 主图等待协议 | load_audit_state 250ms 轮询，30s 超时降级；首轮（state.turn_number == 1）直接全 False 不进等待 |
| D6 | turn 正确性校验 | Redis value 的 turn 必须 == state.turn_number - 1（上一轮）；turn &lt; N-1 视为严重落后降级 + 强日志 |
| D7 | session_notes 写入方式 | 放弃整段重写 4 段骨架方案。改 agentic tool 增量编辑：append_note + replace_in_notes 严格匹配（0/≥2 命中报错），LLM 自决重试或放弃；文体自由不强骨架 |
| D8 | tool agentic loop 形态 | LangGraph 内置 tools_condition + 自写 ToolNode 风格节点；max_audit_tool_iterations=5；超出降级把 LLM 最后一次未应用内容 append 到 session_notes 末尾 + 标注 + 强日志 |
| D9 | tool 返回值 | 成功也返当前全文 `\{"ok": true, "current_notes": "..."\}`；失败返 `\{"error": "...", "current_notes": "..."\}`；与 Claude Code Edit 工具协议一致，LLM 永不脑补 notes 现态 |
| D10 | turn_number 来源 | sessions 表新增 `ai_turn_counter INT NOT NULL DEFAULT 0`（M8 唯一 alembic revision）；persist_ai_turn 同事务 +1；主图入口 state.turn_number = counter + 1 |
| D11 | structured output method | LangChain `with_structured_output(AuditOutputSchema, include_raw=True)`  • `bind_tools([AppendNote, ReplaceInNotes])` 同帧；Step 4 内置 spike 验证 ChatDeepSeek 兼容性 |
| D12 | ARQ 幂等键 | `job_id=f"audit:\{sid\}:\{turn\}"`；同 sid 同 turn 重复入队自动幂等 |
| D13 | DB 写入路径 | audit_records 单次 INSERT + rolling_summaries SELECT FOR UPDATE 读改写 upsert；单事务；WHERE last_turn &lt; :turn 防回退 |
| D14 | 失败重试策略 | ARQ 自带 max_tries=3 指数退避；最终失败 on_job_failure 钩子 SET Redis failed + 日志；audit_records 不写 |
| D15 | ARQ Redis db 隔离 | arq_redis_db=1，应用业务用 db=0；分 db 仅命名空间隔离便于 KEYS 扫描 / FLUSHDB 隔离，不分担容量 |
| D16 | user_stopped 路径 | 仍触发审查（半截 AI 回复也可能含危机/红线信号）；prompt 教 LLM 容忍不完整片段 |
| D17 | 测试基础设施 | fakeredis + `arq.testing.MockArqRedis`；走 `backend/tests/conftest.py` fixture 防御；新增 `pytest.mark.audit` 标签；live spike 标 `pytest.mark.live` 手动触发 |

## 五、Step 拆分

按垂直功能切片 + 每步可独立验证 + 文件增量不返工原则拆分。每 Step 一 commit（Conventional Commits）。

### Step 0 · 路线图顺延 + Notion 计划页登记 + 待办归属修正 ✅

**目标**：阶段 3 启动前的 Notion 维护，与代码无关。

**任务**：

- [x]  [执行规划：17 个里程碑](https://www.notion.so/17-de81294334b947ef8d598245c73832ad?pvs=21) 中 M7.5 / M7.6 章节标题加「⏸ 顺延 M9 之后」标注 + callout 说明（2026-05-14 完成）
- [x]  [M6 · [sse.py](http://sse.py) dev compat 路径 child_profile=None 容忍（M7 Step 0 cleanup 收口）](https://www.notion.so/M6-sse-py-dev-compat-child_profile-None-M7-Step-0-cleanup-82914c6d01b74d12a84b8e2823b449f8?pvs=21) 待办标题改「M7 Step 0 cleanup 收口」+ 备注补归属修正决议（2026-05-14 完成）
- [x]  创建本计划页（本 Step 完成时即同步完成）

**验证**：三处 Notion 修改可见；Iver 审阅 §四决策表全部认可方进 Step 1。

**Commit**：无（纯 Notion 维护）

### Step 1 · 依赖 + 配置基线

**目标**：pyproject 加 arq；[config.py](http://config.py) 加 8 个 audit_ */ arq_* settings。

**任务**：

- [ ]  `backend/pyproject.toml` 加 `"arq>=0.26"` 到 dependencies
- [ ]  `uv lock` 同步 lockfile
- [ ]  `backend/app/config.py` 加 8 个 settings（见关键实现）
- [ ]  `backend/tests/conftest.py` 加 audit_* 默认值环境变量（live 测试除外，默认走 fakeredis）

**关键实现**：

```python
# app/config.py 新增
audit_provider: str = "deepseek"
audit_model: str = "deepseek-v4-flash"
audit_reasoning_effort: str = "max"            # 显式 max；thinking 模式下 low/medium 被 DeepSeek 映射为 high
audit_thinking_enabled: bool = True             # extra_body={"thinking":{"type":"enabled"}}
audit_wait_timeout_seconds: int = 30            # 主图轮询等待上限
audit_redis_ttl_seconds: int = 86400            # 24h signals 管道 TTL
arq_redis_db: int = 1                           # ARQ 队列后端独立 db（应用业务用 db=0）
max_audit_tool_iterations: int = 5              # session_notes tool agentic loop 上限
```

**验证**：

- `pytest backend/tests/test_config.py -k audit` 通过，8 个新增 settings 可读取默认值
- `uv sync` 安装 arq 成功

**Commit**：`chore(m8): add arq dep + audit settings baseline`

### Step 2 · Pydantic schemas + tool schemas

**目标**：`backend/app/schemas/audit.py` 落地全部审查相关 Pydantic 类型。

**任务**：

- [ ]  `AuditDimensionScores`：7 维度字段 × int 0-10（参考 [技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21) §三敏感度维度定义）
- [ ]  `TurnSummaryEntry`：`\{turn_number: int, summary: str (max_length=100), created_at: str ISO-8601\}`
- [ ]  `AuditOutputSchema`：dimension_scores + crisis_detected + crisis_topic + redline_triggered + redline_detail + guidance（≤ 300 字符）+ turn_summary（≤ 100 字符）
- [ ]  `AppendNote` tool schema：`text: str = Field(max_length=500, description="...")`
- [ ]  `ReplaceInNotes` tool schema：`old_str + new_str`，附详细 description 写明唯一匹配语义 + 失败处理
- [ ]  `AuditSignalsPayload`：Redis value JSON 形态（status / turn / signals / started_at / completed_at / error 联合类型）
- [ ]  全 validator 覆盖：summary 长度 / dimension_scores 范围 / crisis_topic 仅在 detected=true 时非空

**验证**：`pytest backend/tests/audit/test_schemas.py` 单元测试 ≥ 12 条覆盖所有 validator 边界（含空值 / 越界 / 不一致字段）

**Commit**：`feat(m8): add audit pydantic schemas + tool schemas`

### Step 3 · Redis 信号管道封装

**目标**：`backend/app/state/audit_signals.py` 封装 `AuditSignalsManager` 三态读写 + turn 校验 + 轮询等待。

**任务**：

- [ ]  新建 `backend/app/state/__init__.py` 替换空 placeholder
- [ ]  新建 `backend/app/state/audit_signals.py`
- [ ]  `AuditSignalsManager` 方法：`set_pending(sid, turn)` / `set_ready(sid, turn, signals)` / `set_failed(sid, turn, error)` / `get(sid)` / `poll_wait(sid, expected_turn, timeout)`
- [ ]  `poll_wait`：250ms 间隔 + deadline + 6 路径分发（ready/pending/failed/miss/turn_mismatch_stale/timeout）
- [ ]  全 Redis 操作走 `redis.asyncio` async API，连接池由 settings 注入

**关键实现**：

```python
async def poll_wait(
    self, sid: str, expected_turn: int, timeout: float
) -> AuditWaitResult:
    deadline = time.monotonic() + timeout
    while True:
        raw = await self._redis.get(f"audit:{sid}")
        if raw is None:
            return AuditWaitResult(kind="miss")
        payload = AuditSignalsPayload.model_validate_json(raw)
        if payload.turn != expected_turn:
            # turn 不匹配：严重落后或 Redis 数据错乱
            return AuditWaitResult(kind="turn_mismatch", actual_turn=payload.turn)
        if payload.status == "ready":
            return AuditWaitResult(kind="ready", signals=payload.signals)
        if payload.status == "failed":
            return AuditWaitResult(kind="failed", error=payload.error)
        # status == pending → 继续等
        if time.monotonic() >= deadline:
            return AuditWaitResult(kind="timeout")
        await asyncio.sleep(0.25)
```

**验证**：`pytest backend/tests/audit/test_audit_signals.py` 覆盖 6 路径（用 fakeredis 模拟各状态 + 时序）

**Commit**：`feat(m8): add audit signals manager + redis three-state pipeline`

### Step 4 · 审查 prompt + LLM 装配 + ChatDeepSeek spike

**目标**：审查 system prompt + LLM 工厂复用 + structured output / tool calls 同帧调用兼容性验证。

**任务**：

- [ ]  `backend/app/audit/__init__.py` 替换空 placeholder
- [ ]  `backend/app/audit/prompts.py`：system prompt + tool 使用协议说明（唯一匹配语义 + 失败处理 + 自由文体不强骨架）
- [ ]  `backend/app/audit/llm.py`：`build_audit_llm(settings)` 复用 `_PROVIDER_REGISTRY` 注册 audit_provider；装配 `with_structured_output(AuditOutputSchema, include_raw=True).bind_tools([AppendNote, ReplaceInNotes])`
- [ ]  Spike（标 `pytest.mark.live`，手动触发，不进 CI）：造 1 个 mock session 2 轮对话 → 验 LLM 一次响应同时拿 AuditOutputSchema 实例 + tool_calls 列表（0/1/2 条）
- [ ]  若 spike 不通过：降级方案 `with_structured_output(method="json_mode")` + 自写 parser + 重试 1 次（在 graph 节点内实现）

**关键实现**：

```python
def build_audit_llm(settings: Settings) -> Runnable:
    base = build_provider_llm(
        provider=settings.audit_provider,
        settings=settings,
        model=settings.audit_model,
        model_kwargs={
            "extra_body": {
                "thinking": {
                    "type": "enabled" if settings.audit_thinking_enabled else "disabled"
                }
            },
            "reasoning_effort": settings.audit_reasoning_effort,
        },
    )
    return base.bind_tools([AppendNote, ReplaceInNotes]).with_structured_output(
        AuditOutputSchema, include_raw=True
    )
```

**验证**：

- 单测：prompt 渲染正确 / tool schema JSON 结构正确
- Live spike：拿 deepseek api key 跑通 1 个 session 2 轮，结构化字段 + tool_calls 同帧返回成功
- 不通过则当场决定降级方案，记入 M8 偏差记录

**Commit**：`feat(m8): add audit llm + system prompt + structured output bindings`

### Step 5 · 审查 LangGraph agentic loop

**目标**：审查图三节点 + tool agentic loop（核心 Step）。

**任务**：

- [ ]  `backend/app/audit/graph.py` 落地审查图
- [ ]  `AuditGraphState` TypedDict：sid / turn_number / messages_context / child_profile / session_notes_working / tool_iter_count / structured_output / pending_tool_calls
- [ ]  `load_context` 节点：从 PG 读 messages 近 N 轮（M9 接闭环时可调；M8 期默认 N=10 与主图对齐）+ `rolling_summaries.session_notes` 当前值 + child_profile
- [ ]  `audit_llm_call` 节点：调 `build_audit_llm` + 传 prompt（含当前 session_notes 作为输入），产 structured_output + tool_calls
- [ ]  `tools_condition` 路由：有 tool_calls → audit_tools；无 → write_results
- [ ]  `audit_tools` 节点（自写）：apply append_note / replace_in_notes 到 session_notes_working，返回 ToolMessage：
    - append 总是成功 → `\{"ok": true, "current_notes": "..."\}`
    - replace 0 命中 → `\{"error": "old_str not found", "current_notes": "..."\}`
    - replace ≥ 2 命中 → `\{"error": "old_str matches N times, please extend context to make it unique", "current_notes": "..."\}`
    - replace 1 命中 → 应用并返 `\{"ok": true, "current_notes": "..."\}`
- [ ]  tool_iter_count += 1 后回到 audit_llm_call（带上 ToolMessage）
- [ ]  循环上限：`tool_iter_count >= settings.max_audit_tool_iterations` 时 audit_tools 节点降级：把最后一次未应用 tool_call 的 text/new_str 内容 append 到 session_notes_working 末尾（带 `[审查 agent 多次尝试修改未果，原始建议如下]\n` 标注）+ 强日志告警 `audit.loop_exceeded sid=... turn=...` → 跳出 loop 进 write_results
- [ ]  `write_results` 节点：调 Step 6 的 writers.write_audit_results

**验证**：`pytest backend/tests/audit/test_audit_graph.py` 覆盖路径：

- 0 tool call（noop）
- 1 append（正常）
- 1 replace 唯一命中（正常）
- 1 replace 0 命中 → LLM 重试用 append → 通过
- 1 replace ≥ 2 命中 → LLM 重试更长 old_str → 通过
- LLM 持续 replace 失败 → 循环超限 → 降级 append + 告警
- 多轮交替 append + replace（验 session_notes_working 状态正确）

**Commit**：`feat(m8): add audit langgraph with agentic tool loop`

### Step 6 · DB 写入路径

**目标**：audit_records 单次 INSERT + rolling_summaries upsert，单事务。

**任务**：

- [ ]  `backend/app/audit/writers.py` 落地 `write_audit_results(db, session_id, turn_number, structured_output, session_notes_final, turn_summary)`
- [ ]  `async with db.begin():` 单事务包裹
- [ ]  INSERT audit_records 全字段（含 dimension_scores JSONB / crisis_detected / crisis_topic / redline_triggered / redline_detail / guidance_injection / turn_number / last_turn 等 M2 已建字段）
- [ ]  SELECT FOR UPDATE rolling_summaries WHERE session_id=:sid
    - 不存在 → INSERT 新行（session_notes=session_notes_final, turn_summaries=[turn_summary_entry], last_turn=:turn）
    - 存在 → UPDATE：append turn_summary_entry 到 turn_summaries / 覆盖 session_notes / 更新 last_turn / crisis_locked 累积（一旦为 true 不可回 false）
    - WHERE last_turn &lt; :turn 防回退
- [ ]  异常回滚不影响 Redis 状态（Redis 由 worker 入口 try/except 在 graph 之外管理）

**验证**：`pytest backend/tests/audit/test_writers.py` 覆盖：

- 首次 INSERT rolling_summaries（无既有行）
- 二次 UPDATE（既有行，append turn_summary）
- turn 回退被拒（current last_turn=5，传入 turn=3 不应覆盖）
- crisis_locked 累积语义（true 不能被 false 覆盖）
- 并发 SELECT FOR UPDATE（两个并发事务串行化）

**Commit**：`feat(m8): add audit_records insert + rolling_summaries upsert with last_turn guard`

### Step 7 · ARQ Worker entrypoint + on_job_failure

**目标**：WorkerSettings + `run_audit` job + 失败钩子。

**任务**：

- [ ]  `backend/app/audit/worker.py` 落地 `WorkerSettings`
- [ ]  WorkerSettings 字段：functions=[run_audit] / redis_settings(db=settings.arq_redis_db) / max_tries=3 / job_timeout=60 / on_job_start / on_job_failure / on_startup（建 db pool / redis）/ on_shutdown
- [ ]  `run_audit(ctx, sid: str, turn_number: int)`：构造 AuditGraphState 初始值 + ainvoke audit_graph + worker 入口 try/except 包裹 → 成功调 AuditSignalsManager.set_ready / 异常向上抛触发 ARQ retry
- [ ]  `on_job_start`：`logger.info("audit.turn.start sid=... turn=...")`
- [ ]  `on_job_failure`（max_tries 用尽）：AuditSignalsManager.set_failed(sid, turn, error) + `logger.error("audit.turn.failed sid=... turn=... err=...")`
- [ ]  job_id 幂等：`audit:\{sid\}:\{turn\}`，同 sid 同 turn 重复入队自动幂等

**验证**：`pytest backend/tests/audit/test_worker.py` 用 `arq.testing.MockArqRedis`：

- enqueue → 同步触发 run_audit → write_results 落库 + Redis ready
- 模拟 LLM 抛错 → retry 3 次 → on_job_failure → Redis failed + audit_records 未写
- 重复 enqueue 同 job_id → 幂等不重跑

**Commit**：`feat(m8): add arq worker + run_audit job + on_job_failure hook`

### Step 8 · ai_turn_counter alembic + persist_ai_turn UPDATE

**目标**：M8 唯一 alembic revision；sessions 表加 ai_turn_counter 字段；persist_ai_turn 同事务 +1。

**任务**：

- [ ]  `cd backend && alembic revision -m "m8: add ai_turn_counter to sessions"`
- [ ]  migration `upgrade`：`ALTER TABLE sessions ADD COLUMN ai_turn_counter INT NOT NULL DEFAULT 0;`
- [ ]  migration backfill（防演练期数据残留，正常 main 实测 ai 消息数都 0 时是 no-op）：`UPDATE sessions SET ai_turn_counter = (SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id AND messages.role = 'ai');`
- [ ]  migration `downgrade`：`ALTER TABLE sessions DROP COLUMN ai_turn_counter;`
- [ ]  `backend/app/models/chat.py` Session ORM 加 `ai_turn_counter: Mapped[int]` 字段
- [ ]  `backend/app/chat/graph.py` `persist_ai_turn` 节点末尾：`UPDATE sessions SET ai_turn_counter = ai_turn_counter + 1 WHERE id = :sid`（同 commit② 事务内）

**验证**：

- `alembic upgrade head` 通过 + `alembic downgrade -1` 回滚通过
- `pytest backend/tests/chat/test_persist_ai_turn.py` 验：连续 3 轮后 counter == 3；并发同 session 不竞态（行锁）
- main 回归 `pytest backend/` 全绿

**Commit**：`feat(m8): add ai_turn_counter to sessions + persist_ai_turn increment`

### Step 9 · 主图 stub 替换 + 等待协议 + turn 校验 + [me.py](http://me.py) 集成

**目标**：替换 M6 三个 stub 节点 + 主图 state 加 turn_number + [me.py](http://me.py) 入口算 turn。

**任务**：

- [ ]  `backend/app/chat/state.py` MainDialogueState 加 `turn_number: int` 字段
- [ ]  `backend/app/chat/graph.py` 替换 `load_audit_state`：
    - `state["turn_number"] == 1` → 直接 return `\{"audit_state": _ALL_FALSE\}` 不进等待
    - 否则 `await audit_signals.poll_wait(sid, expected_turn=N-1, timeout=settings.audit_wait_timeout_seconds)`
    - 按 6 路径分发：ready → 信号注入 / pending → 已在 poll_wait 内等过 → ready 或超时 / failed → 全 False + 日志 / miss → 全 False + 强日志 / turn_mismatch → 全 False + 强日志 / timeout → 全 False + 日志
- [ ]  替换 `enqueue_audit`（改 await）：
    - `await audit_signals.set_pending(sid, turn=N)`
    - `await arq_pool.enqueue_job("run_audit", sid, N, _job_id=f"audit:\{sid\}:\{N\}")`
    - 去掉原 `asyncio.create_task(...)` 包装
- [ ]  替换 `inject_guidance`（M8 期透传 stub，M9 接闭环时真消费）：保留 TODO(M9) 锚点
- [ ]  `backend/app/api/me.py` 主图调用入口：`SELECT sessions.ai_turn_counter` → `state["turn_number"] = counter + 1`（commit② 未跑前的下一轮号）
- [ ]  enqueue_audit 改 await 后 [me.py](http://me.py) 路径 commit② → enqueue_audit → SSE 收尾，多 5-10ms 延迟换 pending 占位写入确定性

**验证**：`pytest backend/tests/chat/test_load_audit_state.py` 覆盖：

- 首轮 → 全 False
- 非首轮 + ready turn=N-1 → 信号注入
- 非首轮 + pending turn=N-1 + 等到 ready → 信号注入
- 非首轮 + pending turn=N-1 + 超时 30s → 全 False + 日志
- 非首轮 + failed turn=N-1 → 全 False + 日志
- 非首轮 + turn &lt; N-1（严重落后）→ 全 False + 强日志
- 非首轮 + miss → 全 False + 强日志
- enqueue_audit 改 await 不破坏 SSE 收尾顺序（commit② 仍在 SSE end 之前完成）

**Commit**：`feat(m8): replace main graph audit stubs with real implementations`

### Step 10 · docker-compose audit_worker service

**目标**：audit_worker 独立 compose service，共享 image，不同 command；backend service 增 env。

**任务**：

- [ ]  `docker-compose.yml` 加 `audit_worker` service：复用 backend service 的 build / image，覆盖 `command: arq app.audit.worker.WorkerSettings`，注入 ARQ_REDIS_DB / DATABASE_URL / REDIS_URL / DEEPSEEK_API_KEY 等 env
- [ ]  backend service 增 `ARQ_REDIS_DB=1` env（供 enqueue 时连同库）
- [ ]  两 service `depends_on: [postgres, redis]`；`restart: always`
- [ ]  audit_worker healthcheck：用 ARQ 自带健康命令或自写探活脚本
- [ ]  `Dockerfile` 不动（共享 image，ENTRYPOINT 不锁死，由 compose `command:` 分发）
- [ ]  `README.md` / `docs/dev.md` 补 audit_worker 启动说明 + 本地开发 `docker compose up audit_worker` 命令

**验证**：

- `docker compose up -d` 后 `docker compose ps` 看到 audit_worker healthy
- `docker compose logs audit_worker` 看到 ARQ worker started 日志 + listening on queue
- 主对话 SSE 走通后 audit_worker 日志收到 audit.turn.start

**Commit**：`chore(m8): add audit_worker compose service + healthcheck`

### Step 11 · 闸门测试 + 偏差记录建页 + 文档回写

**目标**：全链路集成测试 + 偏差记录建页 + 架构基线回写。

**任务**：

- [ ]  `backend/tests/audit/test_e2e.py` 集成测试：模拟 3 轮对话（main graph → enqueue → MockArqRedis 同步触发 audit graph → write_results）→ 验 Redis 三轮 turn=1/2/3 均到 ready + audit_records 3 行 + rolling_summaries last_turn=3 + turn_summaries 长度=3
- [ ]  创建 `M8 · 执行偏差记录` 子页（参考 [M6 · 执行偏差记录](https://www.notion.so/M6-ae216175294b41418ad609103ed3c494?pvs=21) 模板），空模板待实施时填
- [ ]  回写 [技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21) §一审查时序 / §三干预机制 / §四 rolling_summary 字段 / §九审查图节点 / §十三 LLM 客户端：补 M8 实施结论 + agentic loop 决议 + ai_turn_counter 字段引入说明
- [ ]  grep 验收：`grep -rn "TODO(M8)" backend/app/chat` 输出为空（M6 三 stub TODO 锚点已清理）
- [ ]  grep 验收：`grep -rn "audit_state.*Dict\[str, Any\]" backend/app/chat/state.py` 应改为更精确类型（如 `AuditState | None` 或 TypedDict）
- [ ]  全量回归 `pytest backend/` 应有约 80 新增 audit 测试，整体绿（基线 414 → ~494）

**验证**：

- E2E 测试绿
- 偏差记录页可见
- 架构基线 5 个章节回写完成
- 全量 pytest 绿

**Commit**：`test(m8): audit pipeline e2e + cleanup todo markers + arch doc backfill`

## 六、文件清单

**新增**：

```
backend/app/audit/__init__.py           # 替换空 placeholder
backend/app/audit/prompts.py            # 审查 system prompt + tool 协议说明
backend/app/audit/llm.py                # build_audit_llm + with_structured_output + bind_tools
backend/app/audit/graph.py              # 审查 LangGraph agentic loop
backend/app/audit/writers.py            # audit_records / rolling_summaries 写入
backend/app/audit/worker.py             # ARQ WorkerSettings + run_audit job
backend/app/state/__init__.py           # 替换空 placeholder
backend/app/state/audit_signals.py      # AuditSignalsManager 三态读写 + poll_wait
backend/app/schemas/audit.py            # AuditDimensionScores / TurnSummaryEntry / AuditOutputSchema / AppendNote / ReplaceInNotes / AuditSignalsPayload
backend/alembic/versions/m8_ai_turn_counter.py   # 唯一 alembic revision
backend/tests/audit/conftest.py
backend/tests/audit/test_schemas.py
backend/tests/audit/test_audit_signals.py
backend/tests/audit/test_writers.py
backend/tests/audit/test_audit_graph.py
backend/tests/audit/test_worker.py
backend/tests/audit/test_e2e.py
backend/tests/chat/test_load_audit_state.py
backend/tests/chat/test_persist_ai_turn.py
```

**修改**：

```
backend/pyproject.toml                  # +arq
backend/uv.lock                         # uv sync 同步
backend/app/config.py                   # +8 audit_*/arq_* settings
backend/app/chat/state.py               # +turn_number 字段
backend/app/chat/graph.py               # 替换 3 个 M6 stub 节点实体 + persist_ai_turn 增 UPDATE
backend/app/models/chat.py              # +ai_turn_counter ORM 字段
backend/app/api/me.py                   # SELECT counter + 算 turn_number 入 state
docker-compose.yml                      # +audit_worker service
README.md / docs/dev.md                 # audit_worker 启动说明
backend/tests/conftest.py               # audit_* env defaults
```

**完全不动**：

```
backend/app/models/audit.py             # ORM 已完整（M2）
backend/app/chat/context.py             # turn_summaries 注入路径 M9 才接
backend/app/api/sse.py                  # M7 Step 0 收口 dev compat（M8 不动）
Dockerfile                              # 共享 image，由 compose command 分发
```

## 七、配置与依赖变更摘要

**pyproject.toml**：

```toml
"arq>=0.26"  # ARQ 异步任务队列，与 redis[hiredis] 共用 Redis 实例
```

**app/[config.py](http://config.py) 8 个新增 settings**：见 Step 1 关键实现

**docker-compose.yml**：新增 audit_worker service

**alembic**：唯一一次 revision `m8_ai_turn_counter`

## 八、测试边界

| 类型 | 覆盖 | 位置 |
| --- | --- | --- |
| Unit | schemas Pydantic 校验 / signals 三态封装 / writers SQL upsert / prompts 模板渲染 | backend/tests/audit/test_*.py |
| Audit graph integration | fakeredis + db_session fixture，构造 turn 1/2/3 真实数据，跑完三节点 + agentic loop 各路径 | backend/tests/audit/test_audit_[graph.py](http://graph.py) |
| Main graph integration | fakeredis pending→ready 翻转，验 load_audit_state 7 路径（含首轮 / turn_mismatch） | backend/tests/chat/test_load_audit_[state.py](http://state.py) |
| Worker integration | arq.testing.MockArqRedis enqueue + 手触发 run_audit；模拟 retry 用尽落 on_job_failure | backend/tests/audit/test_[worker.py](http://worker.py) |
| E2E | 3 轮对话主图 → enqueue → 审查图 → 落库全链路 | backend/tests/audit/test_[e2e.py](http://e2e.py) |
| Live LLM | 标 [pytest.mark.live](http://pytest.mark.live)，仅手动触发，验 with_structured_output + ChatDeepSeek + tool_calls 同帧调用 | backend/tests/audit/test_llm_[live.py](http://live.py) |

**测试隔离纪律**（沿用 [M6-patch · 测试隔离纪律加固](https://www.notion.so/M6-patch-0636f26e98f94916858983c30fdad01d?pvs=21)）：走 `backend/tests/conftest.py` api_client / db_session / fakeredis / dependency_overrides；新增 `pytest.mark.audit` 标签。

**预期测试增量**：~80 条，基线从 414 → ~494。

## 九、风险与对策

| 风险 | 对策 |
| --- | --- |
| `with_structured_output`  • ChatDeepSeek + tool_calls 同帧调用兼容性未验 | Step 4 内置 live spike；不通过则降级 `method="json_mode"`  • 自写 parser + 重试 1 次 |
| ARQ Redis db 与应用业务 db 混用导致 KEYS 扫描噪声 | 独立 db index（arq=db1 / app=db0），settings 控制 |
| Redis 不可用时主对话 + 审查全炸 | M6 已强依赖 Redis；M8 不引入新依赖；graceful degrade 留 M11+ |
| rolling_summaries 并发写脏（多 worker 同 sid 不同 turn 乱序） | ARQ job_id 幂等 + SELECT FOR UPDATE + WHERE last_turn &lt; :turn 防回退 |
| 审查 worker 卡死导致主图无限等 | 30s 超时 + Redis failed 状态 + 结构化日志告警 |
| tool agentic loop 死循环（LLM 持续 replace 失败） | max_audit_tool_iterations=5 硬上限 + 降级 append 保留信息 |
| session_notes 超长（>8000 字符）触压缩需求 | M8 期会话长度有限不会触；M9/M10 评估自动压缩工具 |
| ai_turn_counter 与 messages 表 ai count 不一致（演练期数据残留） | alembic backfill 一次性同步；后续 persist_ai_turn 单点维护 |
| enqueue_audit 改 await 后 SSE 收尾延迟增加 | 实测 +5-10ms，可接受；换 pending 占位写入确定性 |

## 十、相关文档

- 上下文 / 项目 hub：[青少年 AI 聊天 App · 创业项目](https://www.notion.so/AI-App-3a87ba8cdead4d5e8721efbc24102b9d?pvs=21)
- 设计基线：[M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21)
- 架构基线：[技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21)（§一时序 / §三干预 / §四 rolling_summary / §九审查图 / §十三 LLM 客户端）
- 前置里程碑：[M6 · 主对话链路 - 后端核心 — 实施计划 (6/17)](https://www.notion.so/M6-6-17-a36bdd99fc0f445d86623025c330ea0c?pvs=21) / [M6-patch3 · 上下文阈值压缩 + Session 日切重构 — 实施计划](https://www.notion.so/M6-patch3-Session-79b4cc1c76474b49a3987513d22ad9ad?pvs=21) / [M6 · LLM 抽象重构 + ChatDeepSeek 切换 — 实施计划（M6 收尾补丁）](https://www.notion.so/M6-LLM-ChatDeepSeek-M6-774a4de23a9145d483d7121e241c8450?pvs=21)
- 测试纪律：[M6-patch · 测试隔离纪律加固](https://www.notion.so/M6-patch-0636f26e98f94916858983c30fdad01d?pvs=21)
- 路线图：[执行规划：17 个里程碑](https://www.notion.so/17-de81294334b947ef8d598245c73832ad?pvs=21)
- 后续待办：[](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)
- 编写指引：[Agent 指引 · 实施计划编写](https://www.notion.so/Agent-8edba833b10344dcbb5feb9193161952?pvs=21)

## 十一、阶段 3 完成签收

阶段 3 详细化定稿（2026-05-14）。本页全量可作为 Step-Execute Skill v1.6 入口，按 Step 0 → 11 顺序执行；每 Step 完成后由执行 agent 在本页对应 checkbox 勾选 + Conventional Commits 落 `feat/m8-audit-pipeline` 分支。

**进入 Step-Execute 启动门槛**：本页 §四决策表 + §五 Step 拆分由 Iver 审阅签收。