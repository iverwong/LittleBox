# CLAUDE.md

LittleBox 是一个父子 AI 聊天应用:孩子端走极简聊天,父端管理面板,中间由 AI 做安全审计和日报。

## 目录结构

### 后端

后端按"职责层 + 业务域"二维划分:`core/` 是跨域基础设施(零业务依赖叶子),`domain/` 是业务域(5 域 bounded context)。

```
backend/
├── pyproject.toml           # 依赖与工具配置
├── uv.lock                  # 锁文件
├── Dockerfile               # API 镜像
├── alembic.ini              # 迁移配置
├── alembic/                 # 迁移 (versions/ 在内)
├── scripts/                 # 运维脚本 (DB 清理 SQL / LLM 探测)
├── tests/                   # 测试 (api / audit / chat / integration / runtime / unit)
└── app/                     # 业务代码
    ├── main.py              # FastAPI 工厂
    │
    ├── core/                # 跨域基础设施(零业务依赖)
    │   ├── config.py        # pydantic-settings (env 前缀 LB_)
    │   ├── db.py            # 唯一 engine + async_sessionmaker + Base + BaseMixin + naming_convention
    │   ├── models.py        # ORM 聚合点(显式 import 5 域 13 表,alembic env.py 引此)
    │   ├── enums.py         # 全栈 enum 集中处(UserRole / SubTier / ...)
    │   ├── locks.py         # 通用 Redis 锁(acquire/release_session_lock)
    │   ├── llm.py           # LLM provider 注册表 + 工厂(role × provider)
    │   ├── llm_extractors.py # chunk 字段提取
    │   ├── redis.py         # 唯一 Redis 池 + lifespan + 同步纪律(commit_with_redis / stage_redis_op)
    │   ├── time.py          # 时区工具(SHANGHAI / logical_day)
    │   └── runtime.py       # RuntimeResources 容器 + build_runtime + teardown_runtime
    │
    ├── domain/              # 业务域(bounded context,跨域通信只能走 schemas + 显式事件 + core 基础设施)
    │   ├── accounts/        # 用户/家庭/child profile
    │   │   ├── models.py    # ORM:User / Family / ChildProfile / AuthToken / DeviceToken / FamilyMember / DataDeletionRequest
    │   │   ├── schemas.py   # Pydantic:AccountOut / CreateChildRequest / ...
    │   │   ├── service.py   # create_child / hard_delete_child / age_to_birth_date
    │   │   └── rate_limit.py # login 限流
    │   ├── auth/            # 鉴权子域
    │   │   ├── schemas.py   # LoginRequest / BindTokenResponse / ...
    │   │   ├── deps.py      # get_current_account / require_parent / require_child
    │   │   ├── password.py  # argon2id 哈希
    │   │   ├── tokens.py    # AuthToken 生命周期(issue / resolve / revoke / roll)
    │   │   └── bind_tokens.py # 一次性 bind_token(issue / consume)
    │   ├── chat/            # 主对话域
    │   │   ├── models.py    # ORM:Session / Message
    │   │   ├── schemas.py   # SessionListItem / ChatStreamRequest / ...
    │   │   ├── pipeline.py  # 段一 LLM consumption 协程
    │   │   ├── stream.py    # 段二 SSE 帧转发
    │   │   ├── stream_signals.py # running_streams 进程级 stop event 登记
    │   │   ├── turn_intake.py # commit① 决策矩阵
    │   │   ├── usecase.py    # persist_ai_turn / enqueue_audit
    │   │   ├── context.py   # history 装配
    │   │   ├── context_schema.py # ChatContextSchema frozen dataclass
    │   │   ├── compression.py # M8 上下文压缩
    │   │   ├── history_xml.py # XML 序列化
    │   │   ├── prompts.py   # chat prompt 字符串
    │   │   ├── session_policy.py # 切日规则
    │   │   ├── state.py     # MainDialogueState / AuditState TypedDict
    │   │   └── pagination.py # cursor 编解码
    │   ├── audit/           # 审查 pipeline 域
    │   │   ├── models.py    # ORM:AuditRecord / RollingSummary
    │   │   ├── schemas.py   # AuditDimensionScores / AuditSignalsPayload / ...
    │   │   ├── state.py     # AuditGraphState TypedDict
    │   │   ├── context_schema.py # AuditContextSchema frozen dataclass
    │   │   ├── prompts.py   # 审查 system prompt
    │   │   ├── llm.py       # build_audit_llm(bind_tools + retry + fallback)
    │   │   ├── graph.py     # LangGraph 4 节点 + 2 路由 + 工厂
    │   │   ├── usecase.py   # write_audit_results
    │   │   ├── signals.py   # AuditSignalsManager 三态信号
    │   │   └── worker.py    # ARQ 入口 + run_audit
    │   ├── notifications/   # M10+ 填充
    │   │   ├── models.py    # ORM:Notification
    │   │   └── notify_stub.py # 通知桩
    │   └── expert/          # 日终专家(M12+ 填充)
    │       └── models.py    # ORM:DailyReport
    │
    ├── api/                 # HTTP 协议层(只做协议适配 + 编排,委托 domain.usecase / service)
    │   ├── deps.py          # FastAPI Depends 集(get_db / get_redis / get_current_account)
    │   ├── health.py        # /health
    │   ├── auth.py          # /api/v1/auth/login, /logout(委托 domain.auth + domain.accounts.rate_limit)
    │   ├── bind_tokens.py   # /api/v1/bind-tokens create/status/redeem
    │   ├── children.py      # /api/v1/children CRUD
    │   └── me.py            # /api/v1/me/*(拆薄后,只路由 + 委托)
    │
    ├── scripts/             # CLI 脚本(创建父端 / 重置密码 / 画 mermaid)
    └── artifacts/           # 生成的工件(mermaid,gitignore 内仅保目录)
```

**D-1 边界**:
- `core/*` 零业务依赖,不准 import 任何 `domain/*` / `api/*` / `models/*`
- `api/*` 只 import `core/*` + `domain/*/service|usecase`,不直接 ORM 查询(远期)
- `domain/*` 之间通信只能走 `schemas` + 显式事件 + `core` 基础设施
- `domain/*/graph.py` 不准 import 另一个 domain 的 graph

### 前端 (expo-router)

```
mobile/
├── app/                    # expo-router 文件路由
│   ├── _layout.tsx         # 根布局 + 角色路由
│   ├── index.tsx           # 入口
│   ├── +html.tsx           # HTML 兜底
│   ├── +not-found.tsx      # 404
│   ├── (auth)/             # 登录路由组 (组目录不进 URL)
│   ├── (child)/            # 子端
│   ├── (parent)/           # 父端
│   └── (dev)/              # 开发工具
├── components/             # 共享组件
│   ├── ui/                 # 原子 (Button / Input / Modal / Toast …)
│   ├── layout/             # 布局 (Header / ScreenContainer / ChatBubble)
│   ├── business/           # 业务 (BindQrModal / AgePicker / …)
│   ├── chat/               # 聊天专用
│   ├── icons/              # 图标
│   └── mascot/             # 吉祥物
├── hooks/                  # 自定义 hooks
├── stores/                 # zustand store (auth / chat)
├── services/               # API 客户端 (api/ 子目录 + 顶层)
├── lib/                    # 工具 (SSE 客户端 / streamBuffer / birthDateUtils)
├── theme/                  # 主题 (颜色 / 间距 / ThemeProvider)
├── constants/              # 全局常量
└── assets/                 # 静态资源 (字体 / 插画 / 头像)
```

`(group)` 是 expo-router 的 group 机制,只做路由分组,不影响 URL。

## 运行环境

后端整套跑在 Docker Compose 上(API / PostgreSQL / Redis / pgAdmin / RedisInsight / audit_worker)。本地代码以卷挂载形式进容器,迁移、测试、lint、脚本等所有命令都通过 `docker compose exec api ...` 在容器内执行,不绕过容器直接连库/连服务。

## 工程纪律

### DB / Redis 生命周期管理

**单一进程级容器**: `app/core/runtime.py::RuntimeResources` 集中托管所有进程资源 (db_engine / session_factory / audit_redis / arq_pool / graph / chat task 登记表),FastAPI lifespan 与 ARQ worker 共用同一份构建路径。

**Lifespan 严格顺序** (`app/main.py`):
- startup: `redis_lifespan()` → `build_runtime(settings)` → `app.state.resources = rr`
- shutdown: `_shutdown_wait(rr)`(等候活跃 chat bg task,30s 超时则 cancel)→ `teardown_runtime(rr)`(arq_pool → audit_redis → db_engine 倒序关闭)

**DB session 纪律**:
- 业务 handler 通过 `Depends(get_db)` 拿 session,`yield` 形式,不显式 commit/close
- 业务 commit **必须**走 `commit_with_redis(db, redis)`(`app/core/redis.py`),不要直接 `db.commit()`:
  - 先 DB commit,再 flush 挂载的 Redis ops
  - DB 失败 → ops 丢弃;Redis flush 失败 → log 不抛(缓存可 TTL 自愈)

**Redis 写入纪律**:
- 业务代码不直接 `redis.setex/delete`,改走 `stage_redis_op(db, op)` 挂到 `db.info[pending_redis_ops]`,随 `commit_with_redis` 一起 flush
- session rollback / close 时挂载的 ops 自然丢弃

**Redis 连接纪律**:
- 主 Redis (db=0) + audit Redis (db=`arq_redis_db`)+ arq_pool 三条连接在 lifespan 里统一创建,不要每请求新建客户端
- arq_pool 用 `ArqRedisSettings(host=, port=, password=, database=)` 单独构造(非 URL)
- audit Redis URL 走 `_build_arq_redis_url(settings.redis_url)` 派生,URL 派生单一来源
- 业务通过 `Depends(get_redis)` 拿主 Redis

**测试缝**: 测试可预注入 `app.state.resources` 跳过 lifespan 实际初始化,`shutdown wait` 仍按真实路径跑(#5/#6 测试依赖)

### 全栈异步架构

项目所有 I/O 路径走 async,贯穿依赖 / 运行时 / 应用 / 测试四层。

**依赖层** (`backend/pyproject.toml`):
- DB 驱动: `sqlalchemy[asyncio]` + `asyncpg`(非 psycopg2 同步驱动)
- 缓存: `redis[hiredis]`,走 `redis.asyncio`(非同步 `redis`)
- 任务队列: `arq`(async job queue,非 Celery/RQ)
- LLM: DashScope `AioMultiModalConversation`(异步多模态流式)

**运行时** (`app/core/db.py` / `app/core/runtime.py` / `app/core/redis.py`):
- `create_async_engine` + `async_sessionmaker` + `AsyncSession`
- `redis.asyncio.Redis` + `Redis.from_url` + `aclose`
- 进程资源通过 asyncio 事件循环调度,FastAPI lifespan 与 ARQ worker 共用

**应用层**(路由 + 流式 + 并发):
- 路由 handler 全 `async def`;`yield` 形式注入 `AsyncSession`
- SSE 流式: `async def ... -> AsyncIterator[bytes]` 生成器
- LLM 调用: `async for chunk in llm.astream(messages)` + LangGraph `.astream_events(version="v2")`
- 业务并发原语: `asyncio.Lock` (`app/core/locks.py`) + `asyncio.Event`(stop signal)+ `asyncio.Task`(chat bg task 登记)

**测试层** (`pyproject.toml` + `tests/conftest.py`):
- `asyncio_mode = "auto"` — `async def` 测试自动识别,无需 `@pytest.mark.asyncio` 装饰
- `asyncio_default_fixture_loop_scope = "session"` + `asyncio_default_test_loop_scope = "session"` — fixture 与测试共享 session 级 event loop
- 测试 fixture 全部 `async def` + `pytest_asyncio.fixture`

**CLI 脚本** (`app/scripts/*.py`): 走 `asyncio.run(...)` 入口,复用同一份 `build_runtime` 路径,不在脚本里 new engine / new redis client

### 测试隔离纪律 (M6-patch 起强制)

涉及 DB / Redis 的测试**必须**走 conftest fixture,不允许绕开。

**入口 — fixture 矩阵**:
- `db_session` — 默认选择(savepoint rollback, function scope),覆盖单 session 业务路径
- `concurrent_db_sessions` — N 独立 `AsyncSession`,真 commit + TRUNCATE,仅用于真并发验证场景;**与 `db_session` 互斥**(savepoint 语义不兼容,混用会污染基线)
- `api_client` — ASGI in-process,`dependency_overrides` 注入 `get_db` / `get_redis`
- `redis_client` — fakeredis,function scope,每测试独立实例

**黑名单(禁止)**:
- `subprocess` / `Popen` 跑 `app.scripts.*` 连真实库
- `httpx.Client(base_url="http://localhost:8000")` 直连真 server
- `redis.Redis(host="redis", ...)` 显式连真实 host
- `from app.core.config import settings` 后用 `settings.database_url` 自建 engine
- `flushdb()` / `flushall()`(即便在 fakeredis 里也别用,绕过 fixture 控制)

**双层运行时防御** (`backend/tests/conftest.py`):
- 模块级 `_test_url()` 断言:数据库名必须含 `_test`,否则 pytest 立即 abort(配置错挂的瞬间就 fail,不污染任何 baseline)
- session autouse `_prod_db_row_count_guard`:记录真库行数 baseline,session 结束比对,检测到变化即 fail(拦住"用真库跑了"的漏网情况)

**背景**: M6-patch · 测试隔离纪律加固 (`docs/M6-patch.md`)

### 编码与数据规范

- **HTTPException 状态码**: 必须 `from fastapi import status` + 用 `status.HTTP_xxx_xxx` 常量(例如 `status.HTTP_404_NOT_FOUND`),禁止裸数字 (`HTTPException(404, ...)`)。便于 IDE 跳转/全局审计/改名一处生效。`status_code=...` 在路由装饰器里同样规则 (`status_code=status.HTTP_204_NO_CONTENT`)
- **Python 3.14 PEP 758**: `except` / `except*` 支持不带括号的多个异常类型 (`except A, B:`)
- **`messages.role`**: DB 存 `human`/`ai`(不是 `user`/`assistant`),对齐 LangChain `HumanMessage`/`AIMessage`

### 静态检查与格式

后端用 **ruff**(lint + format)和 **basedpyright**(类型检查,仅 `app/`,不吃 `tests/` 与 `scripts/`),配置都在 `backend/pyproject.toml`。提交前在容器内跑一下:

```
docker compose exec api ruff format
docker compose exec api ruff check
docker compose exec api basedpyright
```

### ORM 聚合点(Phase 6 后)

- `alembic/env.py` 与 `app/main.py` 业务代码统一从 `app.core.models` 拿 `Base` / 13 张表 / 9 个 enum
- `core/models.py` 显式 import 5 域(`accounts` / `audit` / `chat` / `expert` / `notifications`)的 models,**不可漏**;漏一域即 alembic check 产 DROP
- 业务 `from app.models.* import X` 老路径已 sweep 完(Phase 6 commit①),**禁止**新增
