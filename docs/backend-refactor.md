# backend-refactor

# 后端目录结构重构 — 实施计划

> 从 audit 分支头 (`daac7407`) 拉出启动,重构完合并回 `refactor/backend-audit-phase-1`(暂不回 main);**不引入新功能**,只做结构调整与边界收敛。
新结构按”职责层 + 业务域”二维划分,核心思路:把跨域基础设施抽到 `core/`,把混居的 god file 拆薄,把”图节点 / use case / 基础设施”三层分离。
> 

---

## 目标

1. 把 `api/me.py`(~41.6KB / 1034 行)拆成 4 个 chat 域模块,显著拆薄(只留路由 + 委托)
2. 把跨域基础设施(Redis 池 / LLM 工厂 / 锁 / DB↔︎Redis 同步纪律)抽到 `core/`,消除 `auth/`、`chat/`、`audit/` 之间的跨界 import
3. 把 `chat/`、`audit/` 内部”图节点 + use case + 跨域 helper”混居问题修掉
4. 把 `app/models/` 拆到各 `domain/*/models.py`,`app/schemas/` 按端点而非”按 accounts/children”拆
5. 删 `app/services/`、`app/state/`、`app/notify/`、`app/expert/` 临时占位目录
6. scripts 改用 `core.runtime.build_runtime`,与主进程共用一份 engine

## 非目标

- **零行为变更** — 所有功能 / API / 鉴权 / 流式协议 / DB schema / Redis 协议保持不变
- **零 prompt 文案填充** — 14 个 STUB slot 留 prompt 文案专题
- **零通知推送真实化** — `notify_stub` 仍是日志桩,M10+ 落地
- **不引入 repository 层** — 远期 Phase 7 follow-up
- **不重写 LangGraph 拓扑** — 7 节点 / 4 路由 / 4 节点 / 2 路由拓扑锁死
- **不重命名 DB column / table** — 纯代码结构调整

## 前置条件

- **基线 commit**:`daac7407`(audit 分支 `refactor/backend-audit-phase-1` 头,已含 [me.py](http://me.py) 重排 / 字段改名 / [CLAUDE.md](http://CLAUDE.md) 重写 / HTTPException 常量化)
- **新分支**:从 `daac7407` 拉 `backend-restructure`;重构完**合并回 `refactor/backend-audit-phase-1`,暂不回 main**
- **合并纪律**:本次重构在飞期间不再往 `phase-1` 提同一批文件;合回后、删 `backend-restructure` 分支前确认 `git log refactor/backend-audit-phase-1..backend-restructure` 为空(防悬空 commit)
- **测试基线**:`docker compose exec api pytest backend/tests -q` 全绿
- **alembic head**:`docker compose exec api alembic current` 与 `daac7407` 落地 revision 一致
- **容器**:`docker compose up -d` 已起,且审计 worker 与 API 共享同一份 build_runtime
- **决策锁定**:本计划 §关键设计决策 全部对齐全队讨论结论,不再二次决策

---

## 现状问题清单(13 条)

| # | 位置 | 问题 | 严重度 |
| --- | --- | --- | --- |
| 1 | `api/me.py` | ~41.6KB god file,7 职责混居(路由 / cursor / SSE 帧 / 段一协程 / 段二转发 / commit① 矩阵 / session CRUD) | 🔴 高 |
| 2 | `chat/graph.py` | `persist_ai_turn` / `enqueue_audit` 顶层 helper 与 LangGraph 节点混居,被 `me.py` 跨层调用 | 🔴 高 |
| 3 | `chat/factory.py` | 命名错位(audit/ 也用同一份 `_PROVIDER_REGISTRY`),应改名迁 `core/` | 🟡 中 |
| 4 | `state/audit_signals.py` | 目录里只有 1 文件,`AuditSignalsManager` 是 audit 域一部分,不该叫”通用状态” | 🟡 中 |
| 5 | `services/` | 2 文件(`age_converter` / `child_deletion`)与 `domain/accounts/` 重叠 | 🟡 中 |
| 6 | `auth/redis_client.py` | `redis_lifespan` + `_build_arq_redis_url` 跨域,被 `main.py` / `runtime.py` 双调用 | 🟡 中 |
| 7 | `runtime.py` 与 `redis_client.py` | 平行建 Redis 客户端,连接参数各走一遍 urlparse | 🟡 中 |
| 8 | `scripts/_common.py` | `cli_runtime` 自建 engine,绕开 `runtime.build_runtime` | 🟡 中 |
| 9 | `schemas/accounts.py` | 含 children + bind schema,命名/职责错位 | 🟢 低 |
| 10 | `api/auth.py` | 内联 login 限流(常量 + `_check_login_limit` / `_incr_login_fail`) | 🟢 低 |
| 11 | `chat/locks.py` | Redis 锁原语 + `running_streams` 进程级 dict 混 | 🟢 低 |
| 12 | `audit/writers.py` | 通知桩写在审计域(`logger.info("notify.stub...")` 硬编码) | 🟢 低 |
| 13 | `chat/graph.py` 三个 `call_*_llm` | verbatim copy 3 份(只换 LLM 工厂 / provider key / 信号值) | 🟢 低(可读性问题) |

---

## 新目录树(敲定版)

```
backend/app/
│
├── core/                                跨域基础设施层(零业务依赖)
│   ├── config.py                        pydantic-settings
│   ├── enums.py                         ★ 所有 enum 集中处(UserRole / SessionStatus /
│   │                                       MessageStatus / MessageRole / InterventionType /
│   │                                       Gender / DevicePlatform / SubTier /
│   │                                       NotificationType / DailyStatus)
│   ├── db.py                            唯一 engine + async_sessionmaker + get_db + dispose_engine
│   ├── redis.py                         ★ 唯一 Redis 池 + lifespan + 同步纪律
│   │                                       客户端层:_build_arq_redis_url / redis_lifespan /
│   │                                                 get_redis / get_audit_redis
│   │                                       同步层:RedisOp / stage_redis_op /
│   │                                                 discard_pending_redis_ops / commit_with_redis
│   │                                       (从 auth/redis_client.py + auth/redis_ops.py 合并)
│   ├── locks.py                         ★ 通用 Redis 锁(从 chat/locks.py 抽)
│   │                                       acquire_throttle_lock / acquire_session_lock /
│   │                                       release_session_lock(Lua compare-and-delete)
│   ├── llm.py                           ★ LLM provider 注册表 + 工厂(从 chat/factory.py 改名)
│   │                                       _PROVIDER_REGISTRY(去重) / build_provider_llm /
│   │                                       build_main_llm / build_crisis_llm /
│   │                                       build_redline_llm / _build_chat_deepseek /
│   │                                       _build_chat_openai
│   ├── llm_extractors.py                ★ chunk 字段提取(从 chat/extractors.py 迁)
│   │                                       extract_finish_reason / extract_reasoning_content /
│   │                                       extract_usage
│   ├── time.py                          ★ 时区工具(从 chat/session_policy.py 抽工具部分)
│   │                                       SHANGHAI / logical_day
│   └── runtime.py                       RuntimeResources 容器 + build_runtime + teardown_runtime
│
├── api/                                 HTTP 协议层(只做协议适配 + 编排)
│   ├── deps.py                          FastAPI Depends 集(get_db / get_redis /
│   │                                       get_current_account)
│   ├── health.py                        /health
│   ├── auth.py                          /api/v1/auth/login, /logout(拆出限流)
│   ├── bind_tokens.py                   /api/v1/bind-tokens create/status/redeem
│   ├── children.py                      /api/v1/children CRUD
│   └── me.py                            /api/v1/me/*(拆薄后,只路由 + 委托)
│
├── domain/                              业务域(bounded context)
│   ├── accounts/                        用户/家庭/child profile
│   │   ├── models.py                    ORM:User / Family / ChildProfile / AuthToken /
│   │                                       DeviceToken / FamilyMember / DataDeletionRequest
│   │   ├── schemas.py                   Pydantic:AccountOut / CurrentAccount /
│   │                                       LoginRequest / LoginResponse /
│   │                                       CreateChildRequest / ChildSummary /
│   │                                       ListChildrenResponse / ChildProfileOut
│   │   ├── service.py                   create_child / hard_delete_child /
│   │                                       age_to_birth_date / birth_date_to_age
│   │   ├── rate_limit.py                ★ login 限流(从 api/auth.py 抽)
│   │   └── repository.py                远期 follow-up
│   │
│   ├── auth/                            鉴权子域
│   │   ├── deps.py                      require_parent / require_child(从 auth/deps.py 抽)
│   │   ├── password.py                  argon2id 哈希
│   │   ├── tokens.py                    AuthToken 生命周期(issue / resolve / roll /
│   │                                       revoke / revoke_all_active)
│   │   ├── bind_tokens.py               一次性 bind_token(issue / consume / stage_record)
│   │   └── schemas.py                   LoginRequest/Response / AccountOut / CurrentAccount /
│   │                                       BindTokenResponse / CreateBindTokenRequest /
│   │                                       RedeemBindTokenRequest / BindTokenStatusOut
│   │                                       (从原 schemas/accounts.py 拆出 bind 块)
│   │
│   ├── chat/                            主对话域(13 个文件)
│   │   ├── models.py                    ORM:Session / Message
│   │   ├── schemas.py                   Pydantic:SessionListItem / SessionListResponse /
│   │                                       MessageListItem / MessageListResponse /
│   │                                       ChatStreamRequest
│   │   ├── state.py                     MainDialogueState TypedDict + AuditState
│   │   ├── context_schema.py            ChatContextSchema frozen dataclass
│   │                                       (Runtime DI 用,独立不放 schemas.py)
│   │   ├── context.py                   history 装配(main / audit / crisis / redline)
│   │   ├── compression.py               M8 上下文压缩
│   │   ├── history_xml.py               XML 序列化(压缩/锚点用)
│   │   ├── prompts.py                   chat prompt 字符串(单一来源)
│   │   ├── session_policy.py            切日规则(should_switch_session / today_session_title)
│   │   ├── graph.py                     LangGraph 7 节点 + 4 路由 + 工厂
│   │   ├── usecase.py                   ★ persist_ai_turn / enqueue_audit(从 graph.py 抽)
│   │   ├── turn_intake.py               ★ commit① 决策矩阵 Row 1-7(从 me.py 抽)
│   │   ├── pipeline.py                  ★ 段一 LLM consumption 协程(从 me.py 抽)
│   │   ├── stream.py                    ★ 段二 SSE 帧转发 + build_flow_pause_frame
│   │                                       (从 me.py + chat/sse.py 整合)
│   │   ├── stream_signals.py            ★ running_streams 进程级 stop event 登记
│   │   └── pagination.py                ★ cursor keyset 编解码(从 me.py 抽)
│   │
│   ├── audit/                           审查 pipeline 域(10 个文件)
│   │   ├── models.py                    ORM:AuditRecord / RollingSummary
│   │   ├── schemas.py                   Pydantic:AuditDimensionScores / TurnSummaryEntry /
│   │                                       AuditOutputSchema / AppendNote /
│   │                                       ReplaceInNotes / AuditSignalsPayload
│   │   ├── state.py                     AuditGraphState TypedDict
│   │   ├── context_schema.py            AuditContextSchema frozen dataclass
│   │   ├── prompts.py                   审查 system prompt
│   │   ├── llm.py                       build_audit_llm(bind_tools + retry + fallback)
│   │   ├── graph.py                     LangGraph 4 节点 + 2 路由 + 工厂
│   │   ├── usecase.py                   ★ write_audit_results(原 writers.py 合并入此)
│   │   ├── worker.py                    ARQ 入口 + run_audit
│   │   └── signals.py                   AuditSignalsManager 三态信号(从 state/ 迁)
│   │
│   ├── notifications/                   M10+ 填充
│   │   ├── models.py                    ORM:Notification
│   │   ├── schemas.py                   (M10+ 填充)
│   │   └── notify_stub.py               ★ 抽 audit 通知桩
│   │
│   └── expert/                          日终专家
│       └── models.py                    ORM:DailyReport(其余 M11+ 填充)
│
└── scripts/                             CLI 运维
    ├── _common.py                       共享 ArgParser + async runner
    │                                       ★ Phase 5 改用 core.runtime.build_runtime
    ├── create_parent.py
    ├── reset_parent_password.py
    └── draw_graph.py
```

**废弃目录(迁移完成后整体删除):**

- `app/auth/`(分散到 `core/redis.py` + `domain/auth/` + `domain/accounts/`)
- `app/chat/factory.py` → `core/llm.py`
- `app/chat/extractors.py` → `core/llm_extractors.py`
- `app/chat/session_policy.py` 拆分为 `core/time.py` + `domain/chat/session_policy.py`
- `app/state/`(空目录,内容全迁 `domain/audit/signals.py`)
- `app/services/`(折进 `domain/accounts/service.py`)
- `app/models/`(拆到 `domain/*/models.py` + `core/enums.py`)
- `app/schemas/`(拆到 `domain/*/schemas.py`)
- `app/audit/writers.py`(合并到 `domain/audit/usecase.py`)
- `app/notify/`(空占位,仅 `__init__.py`;`domain/notifications/` 全新承接)
- `app/expert/`(空占位,仅 `__init__.py`;`domain/expert/` 全新承接)

---

## 关键设计决策(已锁定)

### D-1:职责层定义

- **`core/*`** 零业务依赖,可被 `api/` / `domain/` / `scripts/` 单向引用
- **`api/*`** 只 import `core/*` + `domain/*/service|usecase`,**不直接 ORM 查询**(远期)
- **`domain/*` 之间通信** 只能通过 `schemas` + 显式事件 + `core` 基础设施
- **`domain/*/graph.py` 不准 import 另一个 domain 的 graph**

### D-2:`usecase.py` 边界

- **装**:跨表事务 / 跨外部服务(DB + Redis + arq)的事务编排
- **不装**:纯算法(prompt 拼装、age 换算、SSE 帧生成)放 chat/ 域内对应模块
- **不装**:单一 Repo 操作(远期拆出 repository 时放 Repo)
- **不装**:LangGraph 节点 / 路由函数(放 graph.py)

### D-3:`audit/writers.py` 合并进 `audit/usecase.py`

不再单设 `audit/persistence.py`。`write_audit_results` 的”通知桩”在 D-5 抽到 `domain/notifications/notify_stub.py`。

### D-4:`redis_client.py` + `redis_ops.py` 合并进 `core/redis.py`

两文件加起来 ~100 行,合并后可读且减少文件数。模块 docstring 标明”客户端层 + 同步层”两个职责。

### D-5:通知桩抽到 `domain/notifications/notify_stub.py`

`audit/usecase.py` 末尾 `logger.info("notify.stub.crisis ...")` 改为 `notify_stub.send(notify_type, sid, turn, target)`,真实推送 M10+ 替换 `notify_stub.send` 内部实现。

### D-6:scripts 必须走 `core.runtime.build_runtime`

`scripts/_common.py::cli_runtime` 删除自建 engine / redis 客户端代码,改为:

```python
@asynccontextmanager
async def cli_runtime() -> AsyncIterator[tuple[RuntimeResources, AsyncSession]]:
    rr = await build_runtime(settings)
    async with rr.db_session_factory() as session:
        yield rr, session
```

### D-7:`core/llm.py` 去重

`chat/factory.py` 现状 5 条注册表 (`deepseek` / `openai` / `audit_deepseek` / `audit_bailian` / `compression_deepseek`)合并为”role × provider”二维工厂:

```python
def build_provider_llm(role: Literal["main", "audit", "compression"],
                       provider: str, settings) -> Runnable:
    """role 决定 model / thinking / reasoning;provider 决定 base_url / api_key。"""
```

5 条 lambda → 1 个工厂 + role 维度参数。

### D-8:三个 `call_*_llm` verbatim copy 抽 `_stream_llm_chunks`

`chat/graph.py` 三个 call_*_llm 抽公共协程,见 §Phase 3 step。

---

## 边界铁律(给后续约束)

1. **`core/*` 零业务依赖** — 不准 import `domain/*` / `api/*` / `models/*`
2. **`api/*` 只 import `core/*` + `domain/*/service|usecase`** — 不直接 ORM(分阶段)
3. **`domain/*` 之间通信** — 只能 `schemas` + 事件 + `core` 基础设施
4. **`scripts/*` 必须走 `core.runtime.build_runtime`** — 禁自建 engine / redis

---

## 迁移阶段(7 phases)

按”低风险纯搬迁 → 中风险结构调整 → 高风险改造”排序。每 phase 单独 PR,出问题时 revert 单 phase 即可。

### Phase 1:低风险纯搬迁(预计 0.5 天)

**目标**:不动业务逻辑,只做文件搬位置 + 拆 schemas。

| Step | 任务 | 验证 | Commit message |
| --- | --- | --- | --- |
| 1.1 | 拆 `schemas/accounts.py` → `domain/accounts/schemas.py` + `domain/auth/schemas.py` | `grep -r "from app.schemas.accounts" backend/app` 全部替换,`pytest` 全绿 | `refactor(schemas): 拆 accounts/children/bind 按端点` |
| 1.2 | 迁 `state/audit_signals.py` → `domain/audit/signals.py` | 改 import 路径,`pytest` 全绿 | `refactor(audit): 迁 AuditSignalsManager 到 domain/audit/` |
| 1.3 | 折 `services/` → `domain/accounts/service.py` | `service.py` 含 `age_to_birth_date` / `birth_date_to_age` / `create_child` / `hard_delete_child` | `refactor(accounts): 折 services/ 进 domain/accounts/service` |
| 1.4 | 抽 `api/auth.py` 限流 → `domain/accounts/rate_limit.py` | `_check_login_limit` / `_incr_login_fail` 迁出,`api/auth.py` 委托调用 | `refactor(auth): 抽 login 限流到 domain/accounts/rate_limit` |

**验证清单**:
- [ ] `pytest backend/tests -q` 全绿
- [ ] `grep -r "app.services" backend/app` 无结果
- [ ] `grep -r "app.state" backend/app` 无结果
- [ ] `grep -r "from app.schemas.accounts import" backend/app` 仅出现在 `app/domain/accounts/` 与 `app/domain/auth/` 内部

### Phase 2:抽 me.py(预计 1-2 天)

**目标**:把 `api/me.py`(~41.6KB / 1034 行)拆薄,4 个 chat 域模块承担具体职责。

| Step | 任务 | 验证 | Commit message |
| --- | --- | --- | --- |
| 2.1 | 抽 `_encode_cursor` / `_decode_cursor` / `InvalidCursor` → `domain/chat/pagination.py` | me.py 减 ~80 行,游标分页测试通过 | `refactor(chat): 抽 cursor 编解码到 pagination.py` |
| 2.2 | 抽 `_frame_sse_event` / `_stream_generator` / `_ChatStreamState` + `chat/sse.py::build_flow_pause_frame` / `stream_graph_to_sse` → `domain/chat/stream.py` | me.py 减 ~80 行,流式协议测试通过 | `refactor(chat): 抽 SSE 帧生成与段二转发到 stream.py` |
| 2.3 | 抽 `_run_llm_pipeline` → `domain/chat/pipeline.py` | me.py 减 ~300 行,段一协程独立可测 | `refactor(chat): 抽段一 LLM 协程到 pipeline.py` |
| 2.4 | 抽 commit① 决策矩阵(Row 1-7)→ `domain/chat/turn_intake.py`。**非纯搬运**:矩阵内联在 `chat_stream` 体内、与锁获取 / ctx / initial_state 构造共享大量局部变量,需先定义 `TurnIntakeResult`(dataclass)作返回载体,属等价重写 | `test_chat_stream_*`(control_plane / lifecycle / stop_keepgo)全绿,Row 1-7 行为不变 | `refactor(chat): 抽 commit① 决策矩阵到 turn_intake.py` |
| 2.5 | 顺手:[me.py](http://me.py) 残留裸数字 `HTTPException(404/403, ...)` 改 `status.HTTP_*` 常量(commit 2 把 [me.py](http://me.py) 的常量化显式留给本次重构) | `grep -rnE "HTTPException\([0-9]" backend/app/api/me.py` 无结果 | `refactor(me): HTTPException 裸数字改 status 常量` |

**me.py 最终保留**:
- 路由注册(7 个):`get_me` / `get_my_profile` / `list_sessions` / `get_messages` / `delete_session` / `stop_session` / `chat_stream`
- 业务编排:`chat_stream` 委托给 `chat.turn_intake.intake_human_message` + `chat.pipeline.run_llm_pipeline`

**验证清单**:
- [ ] [me.py](http://me.py) 仅保留 7 个路由注册 + 委托编排(无 cursor / SSE / 段一 / 段二 / 决策矩阵 残留)
- [ ] 集成测试(Step 7 chat 流式)全绿
- [ ] 7 个 chat 路由响应字段零变化(diff 现有 snapshot 测试)

### Phase 3:拆 chat 内部 + 抽 helper(预计 1 天)

**目标**:把 `chat/locks.py` 拆,`chat/graph.py` 顶层 helper 抽到 usecase。

| Step | 任务 | 验证 | Commit message |
| --- | --- | --- | --- |
| 3.1 | 拆 `chat/locks.py` → `core/locks.py`(Redis 锁)+ `domain/chat/stream_signals.py`(`running_streams` 登记表) | 锁原语测试通过;stop signal 行为不变 | `refactor(core): 拆 chat/locks.py 到 core/locks + chat/stream_signals` |
| 3.2 | 抽 `chat/graph.py::persist_ai_turn` → `domain/chat/usecase.py` | graph.py 减 ~50 行;usecase 独立可测 | `refactor(chat): 抽 persist_ai_turn 到 usecase.py` |
| 3.3 | 抽 `chat/graph.py::enqueue_audit` → `domain/chat/usecase.py` | 字面量 `"app.audit.worker.run_audit"` 移到 usecase 顶部常量,graph.py 不再 import arq / audit_redis | `refactor(chat): 抽 enqueue_audit 到 usecase.py` |
| 3.4 | 抽三个 `call_*_llm` 公共部分 → `domain/chat/_stream_llm_chunks` 私有协程 | graph.py 减 ~120 行,3 个 call_*_llm 退化为 ~20 行委派 | `refactor(chat): 抽 _stream_llm_chunks 公共协程,合并 3 个 call_*_llm` |

**验证清单**:
- [ ] [graph.py](http://graph.py) 仅保留 LangGraph 节点 + 路由 + 工厂(helper 已抽 usecase)
- [ ] 5 path 路由测试(main / crisis-lock / crisis-恢复 / redline / guidance)全绿
- [ ] LangSmith trace 字段零变化(`run_name` / `metadata` / `tags` 不变)

### Phase 4:工厂改名 + 跨域合并(预计 1-2 天)

**目标**:`core/llm.py` 去重;`core/redis.py` 合并两个旧文件;通知桩抽离。

| Step | 任务 | 验证 | Commit message |
| --- | --- | --- | --- |
| 4.1 | `chat/factory.py` 改名 + 迁 `core/llm.py`,5 条注册合并为”role × provider”工厂(见 D-7) | `build_provider_llm("main", "deepseek", s)` / `("audit", "deepseek", s)` / `("compression", "deepseek", s)` 三调用路径返回预期 LLM | `refactor(core): 迁 chat/factory.py 到 core/llm.py 并去重 5 条注册表` |
| 4.2 | `chat/extractors.py` → `core/llm_extractors.py`(纯迁) | `extract_finish_reason` / `extract_reasoning_content` / `extract_usage` 调用点全部更新 | `refactor(core): 迁 extractors.py 到 core/llm_extractors.py` |
| 4.3 | `chat/session_policy.py` 拆 `core/time.py`(`SHANGHAI` / `logical_day`)+ `domain/chat/session_policy.py`(`should_switch_session` / `today_session_title`) | 时区工具测试通过,切日规则测试通过 | `refactor(time): 拆 session_policy 到 core/time + chat/session_policy` |
| 4.4 | `auth/redis_client.py` + `auth/redis_ops.py` 合并迁 `core/redis.py`(见 D-4) | `redis_lifespan` / `get_redis` / `get_audit_redis` / `commit_with_redis` 全部从 `core.redis` 出 | `refactor(core): 合并 auth/redis_client + auth/redis_ops 到 core/redis` |
| 4.5 | 抽 `audit/writers.py::notify stub` → `domain/notifications/notify_stub.py`,`audit/usecase.py` 改注入调用(见 D-5) | `notify_stub.send("crisis" / "redline", ...)` 调用,日志格式与原 logger.info 一致 | `refactor(notifications): 抽 notify stub 到 domain/notifications/` |
| 4.6 | `audit/writers.py` 合并进 `audit/usecase.py`(见 D-3) | `write_audit_results` 内容全迁,`audit/writers.py` 文件删除 | `refactor(audit): 合并 writers.py 到 usecase.py` |

**验证清单**:
- [ ] `grep -r "from app.chat.factory" backend/app` 无结果(全部走 `from app.core.llm`)
- [ ] `grep -r "from app.chat.extractors" backend/app` 无结果(全部走 `from app.core.llm_extractors`)
- [ ] `grep -r "from app.auth.redis" backend/app` 无结果(全部走 `from app.core.redis`)
- [ ] `core/llm.py` 去重为等价重写:`test_factory.py` 全绿,§十三 finish_reason / openai 协议约定不变
- [ ] LLM Provider 探针补 4 用例全绿
- [ ] Redis 客户端只在 `core/redis.py` 创建一份

### Phase 5:scripts 统一 runtime(预计 0.5 天)

**目标**:`scripts/_common.py::cli_runtime` 改用 `core.runtime.build_runtime`。

| Step | 任务 | 验证 | Commit message |
| --- | --- | --- | --- |
| 5.1 | `scripts/_common.py::cli_runtime` 改用 `build_runtime`,返回 `(RuntimeResources, AsyncSession)` | 三个 CLI 脚本(create_parent / reset_parent_password / draw_graph)走新路径,行为不变 | `refactor(scripts): 改用 core.runtime.build_runtime,不再自建 engine` |

**验证清单**:
- [ ] `docker compose exec api python -m app.scripts.create_parent --help` 输出正常
- [ ] CLI 实跑一次父账号创建,行为与重构前一致

### Phase 6:迁移聚合点 + 删除老目录(预计 0.5-1 天)

**目标**:先迁 alembic 元数据聚合点,再整体删除废弃目录,最后同步 [CLAUDE.md](http://CLAUDE.md)。

| Step | 任务 | 验证 | Commit message |
| --- | --- | --- | --- |
| 6.0 | ★ **删 `app/models/` 前置**:`base.py::Base` 迁 `core/db.py` 后,新建模型聚合点(`core/db.py` 末尾或独立 `core/models.py` import 全部 `domain/*/models.py`),并把 `alembic/env.py` 的 `from app.models.base import Base` 改为 `from app.core.db import Base`,确保 `target_metadata` 看到全部 13 张表 | `docker compose exec api alembic check` 无未生成迁移(metadata 看到全部表,不产 DROP) | `refactor(db): 迁 Base 到 core/db,建模型聚合点并改 alembic env` |
| 6.1 | 删除 `app/auth/`(内容已迁 `core/redis.py` + `domain/auth/` + `domain/accounts/service.py`) | `grep -r "from app.auth" backend/app` 无结果 | `refactor: 删除 app/auth/ 旧目录` |
| 6.2 | 删除 `app/services/`(内容已折 `domain/accounts/service.py`) | 目录为空,git rm | `refactor: 删除 app/services/ 旧目录` |
| 6.3 | 删除 `app/state/`(内容已迁 `domain/audit/signals.py`) | 目录为空,git rm | `refactor: 删除 app/state/ 旧目录` |
| 6.4 | 删除 `app/models/`(内容已拆 `domain/*/models.py`  • `core/enums.py`,聚合点见 6.0) | `grep -r "from app.models" backend/app backend/alembic` 无结果([env.py](http://env.py) 已改 core.db) | `refactor: 删除 app/models/ 旧目录` |
| 6.5 | 删除 `app/schemas/`(内容已拆 `domain/*/schemas.py`) | `grep -r "from app.schemas" backend/app` 无结果 | `refactor: 删除 app/schemas/ 旧目录` |
| 6.6 | 删除 `app/notify/`、`app/expert/` 空占位(`domain/notifications/`、`domain/expert/` 已全新承接) | 两目录 git rm;`grep -r "from app.notify\|from app.expert" backend/app` 无结果 | `refactor: 删除 app/notify/ 与 app/expert/ 空占位` |
| 6.7 | ★ 同步 `CLAUDE.md`:目录结构段改为新二维布局;修工程纪律里失真的路径引用(`chat/locks.py` / `auth/redis_client.py` / `auth/redis_ops.py` / `scripts/*` 等) | [CLAUDE.md](http://CLAUDE.md) 目录树与 §新目录树 一致,工程纪律路径全部指向 `core/*` / `domain/*` | `docs: CLAUDE.md 对齐 M10 二维目录结构` |

**验证清单**:
- [ ] `app/` 目录树与本计划 §新目录树 完全一致
- [ ] `pytest backend/tests -q` 全绿
- [ ] `docker compose exec api alembic check` 通过(无未生成迁移)
- [ ] `grep -r "from app\." backend/app` 仅命中 `app.core.*` / `app.domain.*` / `app.api.*` / `app.scripts.*`
- [ ] `CLAUDE.md` 目录树 + 工程纪律路径已同步

### Phase 7:repository 层(远期,本期不实现)

**目标**:引入 `domain/accounts/repository.py` / `domain/chat/repository.py`,把路由层直接 ORM 查询封装到 Repo。

| Step | 任务 | 验证 | Commit message |
| --- | --- | --- | --- |
| 7.1 | (远期)建 `domain/accounts/repository.py`:`FamilyRepository.lock_for_update` / `count_children` 等 | 路由层不再 `select(User).where(...)` | (follow-up M10+) |
| 7.2 | (远期)建 `domain/chat/repository.py`:`MessageRepository` / `SessionRepository` | `me.py` / `turn_intake.py` 不再直接 ORM | (follow-up M10+) |

**本期不实现**,留作 M10+ 独立 PR。

---

## 关键迁移对应表(老 → 新)

| 老位置 | 新位置 | Phase |
| --- | --- | --- |
| `app/auth/redis_client.py` | `app/core/redis.py`(合并) | 4.4 |
| `app/auth/redis_ops.py` | `app/core/redis.py`(合并) | 4.4 |
| `app/auth/deps.py::require_parent/require_child` | `app/domain/auth/deps.py` | 1.x + 4.x 渐进 |
| `app/auth/password.py` / `tokens.py` / `bind.py` | `app/domain/auth/password.py` / `tokens.py` / `bind_tokens.py` | 4.x 渐进 |
| `app/chat/locks.py` Redis 锁部分 | `app/core/locks.py` | 3.1 |
| `app/chat/locks.py::running_streams` | `app/domain/chat/stream_signals.py` | 3.1 |
| `app/chat/factory.py` | `app/core/llm.py`(改名 + 去重) | 4.1 |
| `app/chat/extractors.py` | `app/core/llm_extractors.py` | 4.2 |
| `app/chat/session_policy.py::SHANGHAI/logical_day` | `app/core/time.py` | 4.3 |
| `app/chat/session_policy.py::should_switch_session/today_session_title` | `app/domain/chat/session_policy.py` | 4.3 |
| `app/chat/graph.py::persist_ai_turn` | `app/domain/chat/usecase.py` | 3.2 |
| `app/chat/graph.py::enqueue_audit` | `app/domain/chat/usecase.py` | 3.3 |
| `app/chat/graph.py` 三个 `call_*_llm` 公共部分 | `app/domain/chat/graph.py::_stream_llm_chunks` 私有协程 | 3.4 |
| `app/chat/sse.py` | `app/domain/chat/stream.py`(合并) | 2.2 |
| `app/api/me.py` cursor 编解码 | `app/domain/chat/pagination.py` | 2.1 |
| `app/api/me.py` SSE 帧 + 段二 | `app/domain/chat/stream.py` | 2.2 |
| `app/api/me.py` 段一协程 | `app/domain/chat/pipeline.py` | 2.3 |
| `app/api/me.py` commit① 矩阵 | `app/domain/chat/turn_intake.py` | 2.4 |
| `app/api/auth.py` 限流常量 + helper | `app/domain/accounts/rate_limit.py` | 1.4 |
| `app/services/age_converter.py` | `app/domain/accounts/service.py` | 1.3 |
| `app/services/child_deletion.py` | `app/domain/accounts/service.py` | 1.3 |
| `app/state/audit_signals.py` | `app/domain/audit/signals.py` | 1.2 |
| `app/models/accounts.py` | `app/domain/accounts/models.py` | 6.4 |
| `app/models/chat.py` | `app/domain/chat/models.py` | 6.4 |
| `app/models/audit.py` | `app/domain/audit/models.py` | 6.4 |
| `app/models/parent.py` | 拆到 `accounts/models.py` + `notifications/models.py` + `expert/models.py` | 6.4 |
| `app/models/enums.py` | `app/core/enums.py` | 6.4 |
| `app/models/base.py` | `app/core/db.py`(Base 与 engine 共置) | 6.4 |
| `app/schemas/accounts.py`(children 部分) | `app/domain/accounts/schemas.py` | 1.1 |
| `app/schemas/accounts.py`(auth/bind 部分) | `app/domain/auth/schemas.py` | 1.1 |
| `app/schemas/children.py` | `app/domain/accounts/schemas.py`(合并) | 1.1 |
| `app/schemas/sessions.py` | `app/domain/chat/schemas.py` | 1.1 |
| `app/schemas/audit.py` | `app/domain/audit/schemas.py` | 1.1 |
| `app/audit/writers.py` | `app/domain/audit/usecase.py`(合并) | 4.6 |
| `app/audit/writers.py` notify stub | `app/domain/notifications/notify_stub.py` | 4.5 |
| `app/scripts/_common.py::cli_runtime` | 改用 `app/core/runtime.py::build_runtime` | 5.1 |

---

## 验证策略

### 每 Phase 必跑

```bash
# 1. 全量单元 + 集成测试
docker compose exec api pytest backend/tests -q

# 2. lint + 类型
docker compose exec api ruff check backend/app
docker compose exec api mypy backend/app  # 可选

# 3. import 路径健康度(检测未迁移完的旧引用)
docker compose exec api python -c "
import re, pathlib
root = pathlib.Path('backend/app')
bad = []
for p in root.rglob('*.py'):
    text = p.read_text(encoding='utf-8')
    for m in re.finditer(r'from app\.(\w+)', text):
        mod = m.group(1)
        if mod in {'auth', 'services', 'state', 'models', 'schemas', 'notify', 'expert'}:
            bad.append(f'{p}: app.{mod}')
        if mod == 'chat' and ('factory' in text or 'extractors' in text):
            bad.append(f'{p}: app.chat.factory/extractors')
if bad:
    print('Found legacy imports:')
    for b in bad: print(' ', b)
    raise SystemExit(1)
print('OK: no legacy imports')
"

# 4. alembic schema 一致性
docker compose exec api alembic check
```

### 端到端冒烟(每 Phase 完成后)

| 流程 | 验证点 |
| --- | --- |
| 父端登录 | `POST /api/v1/auth/login` 返回 200 + token,Redis login_fail 桶清零 |
| 父端创建 child | `POST /api/v1/children` 201,DB 出现 users/child_profiles/family_members 行 |
| 父端发 bind_token | `POST /api/v1/bind-tokens` 200,Redis bind: 前缀出现 |
| 子端 redeem | `POST /api/v1/bind-tokens/{token}/redeem` 200,Redis bind_result: 出现 |
| 子端 chat_stream | `POST /api/v1/me/chat/stream` SSE 流正常,`audit: 出现 → ready` 管道走通 |
| 父端查 children | `GET /api/v1/children` 200,is_bound 正确 |
| 父端删 child | `DELETE /api/v1/children/{id}` 204,DB CASCADE,DataDeletionRequest 写入 |

### 关键回归测试

- `tests/chat/test_decision_matrix.py`(commit① 7 行)
- `tests/chat/test_pipeline.py`(段一三终态)
- `tests/chat/test_stream.py`(段二 overflow / 客户端断)
- `tests/audit/test_write_audit_results.py`(原 writers 单元)
- `tests/auth/test_tokens.py`(commit_with_redis 同步纪律)
- `tests/integration/test_chat_e2e.py`(M9.5 端到端)

---

## 风险与回滚

| 风险 | 触发条件 | 回滚策略 |
| --- | --- | --- |
| 跨包 import 漏改 | 启动 ImportError / `pytest` import 阶段失败 | Phase 各自独立 PR,`git revert <phase-PR>` 即可 |
| chat 路由行为漂移 | 集成测试 / 端到端冒烟失败 | Phase 2 是最大风险,拆 4 个 step 逐步提交,任一 step 出问题单独 revert |
| `core/llm.py` 去重引回归 | LLM 探针补 4 / 5 path 路由测试失败 | 回滚 Phase 4.1,保留 5 条注册表;不强推去重 |
| scripts 改造引 CLI 失败 | `app.scripts.*` 跑不动 | 回滚 Phase 5.1,`_common.py::cli_runtime` 恢复自建 engine |
| alembic 看不到表 → autogenerate 产 DROP | `alembic check` 报错 / 生成 DROP TABLE | 删 `app/models/` 后 [env.py](http://env.py) 仍引旧路径、或聚合点漏 import 某域 model 即触发(见 Phase 6.0)。回滚 6.0 / 6.4,恢复 `app/models/__init__.py` 聚合 + [env.py](http://env.py) 旧 import |

### 渐进式回滚保证

- 每 Phase 1 个 PR,共 6 个 PR
- 任一 Phase 出问题不影响其他 Phase
- `app/auth/` / `app/services/` / `app/state/` / `app/models/` / `app/schemas/` 在 Phase 6 之前**保留**,可在过渡期回滚到旧 import 路径
- `app/chat/` 旧模块(除 factory / extractors)保留到 Phase 6 整体删除,任何 chat 域回滚都能 revert 回 me.py 单文件

---

## 执行步骤总览(按 PR 切)

| PR | Phase | 估时 | 风险 | 关键 commit 标题 |
| --- | --- | --- | --- | --- |
| PR-1 | Phase 1.1-1.4 | 0.5 天 | 🟢 低 | `refactor: 拆 schemas + 迁 services/state/rate_limit(4 commit)` |
| PR-2 | Phase 2.1-2.4 | 1-2 天 | 🟡 中 | `refactor(chat): 拆 me.py 4 个模块(4 commit)` |
| PR-3 | Phase 3.1-3.4 | 1 天 | 🟡 中 | `refactor(chat): 拆 locks + 抽 usecase + 合并 3 call_*_llm(4 commit)` |
| PR-4 | Phase 4.1-4.6 | 1-2 天 | 🟡 中 | `refactor(core): 工厂去重 + 合并 redis + 抽 notify_stub + 合并 audit/usecase(6 commit)` |
| PR-5 | Phase 5.1 | 0.5 天 | 🟢 低 | `refactor(scripts): 改用 core.runtime.build_runtime` |
| PR-6 | Phase 6.0-6.7 | 0.5-1 天 | 🟡 中 | `refactor: 迁 alembic 聚合点 + 删 7 个旧目录 + 同步 CLAUDE.md` |

**总估时:6-8 个工作日,6 个 PR**。

---

## 与历史 milestone 的衔接

- **M9 主体期**(commit `60c772d`):已落 LangGraph 拓扑 + Runtime DI,本次重构不动
- **M9.5 集成测试**(`e4f77b2`):提供端到端冒烟基线,Phase 完成后跑 `tests/integration/`
- **audit phase-1**(`daac7407`):本次重构的起点;合并回该分支,暂不回 main
- **M10+ 真实通知推送**:`domain/notifications/notify_stub.py` 是预留接缝,届时替换 `notify_stub.send` 实现即可,不动 audit/usecase
- **M10+ prompt 文案**:不动,14 STUB slot 保持
- **M11+ repository 层**:Phase 7 远期,本计划锁定
- **M12+ 日终专家**:`domain/expert/` 目录已建,内容待 M12 填充

---

## 后续重构条目

- **Natural-Day Session 切日重构**(`refactor/expert-first-human-message-restructuring` 分支):
  - `app/chat/session_policy.py` 改为自然日 + 跨日 30min 宽限 + 04:00 硬切(R1 / R2 / R3')。
  - `app/api/me.py` 两处 `should_switch_session` 调用点传入 `latest.created_at`。
  - `app/core/time.py` 新增 `same_natural_day` 纯函数。
  - `app/expert/worker.py` 抽 `_compute_window(now)`,锚定自然日 `[T-1 00:00, T0 00:00)`,消除 chat↔expert 切日错位。

---

## 验收门槛

- **必达**:6 个 PR 全部 merge 回 `refactor/backend-audit-phase-1`(暂不回 main),`pytest` 全绿,5 path 路由 + commit① 7 行 + 集成测试 + 端到端冒烟 全部通过
- **必达**:`grep -r "from app\.auth\|from app\.services\|from app\.state\|from app\.models\|from app\.schemas\|from app\.notify\|from app\.expert" backend/app` 无结果([env.py](http://env.py) 已改 `core.db`,不再列例外)
- **必达**:`app/` 目录树与本计划 §新目录树 一致
- **必达**:`alembic check` 无未生成迁移;`CLAUDE.md` 目录树 + 工程纪律路径已同步
- **稳定锚点(替代行数指标)**:每步以 pytest 全绿 + basedpyright 0/0/0 + import 单向(无 legacy 引用,见 §验证策略 import 健康度脚本)+「旧目录已删 / 目录树吻合」为完成信号;以行为不变为核心,不再用行数硬卡
- **可选**:M10+ follow-up(repository 层 / 真实通知 / prompt 文案)按各自 PR 推进