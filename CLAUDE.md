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
    ├── main.py              # FastAPI 工厂与 lifespan 编排
    ├── worker.py            # ARQ Worker 入口(聚合 audit + expert job + cron)
    │
    ├── core/                # 跨域基础设施(零业务依赖)
    │   ├── config.py        # pydantic-settings(env 前缀 LB_)
    │   ├── db.py            # get_db 入口;Base / BaseMixin / 命名约定
    │   ├── enums.py         # 全栈 enum 集中处
    │   ├── models.py        # ORM 聚合点(显式 import 5 域 13 张表)
    │   ├── time.py          # 时区工具与 logical_day
    │   ├── locks.py         # 通用 Redis 锁
    │   ├── llm.py           # LLM 工厂(transport adapter + Runtime DI)
    │   ├── llm_topology.py  # Role / ModelProfile / Endpoint + resolve_profile
    │   ├── llm_extractors.py # LLM chunk 字段提取
    │   ├── history_xml.py   # 对话历史 XML 序列化
    │   ├── redis.py         # 主 Redis 池 + staging 纪律(commit_with_redis)
    │   └── runtime.py       # RuntimeResources 容器 + build/teardown
    │
    ├── domain/              # 业务域(跨域通信只走 schemas + 显式事件 + core)
    │   ├── accounts/        # 用户/家庭/child profile
    │   ├── auth/            # 鉴权子域(tokens / bind_tokens / password)
    │   ├── chat/            # 主对话域
    │   │   ├── models.py    # ORM:Session / Message
    │   │   ├── schemas.py   # Pydantic schema
    │   │   ├── pipeline.py  # LLM consumption 协程(图前发 session_meta,图后 commit②)
    │   │   ├── stream.py    # SSE 帧转发
    │   │   ├── stream_signals.py # running_streams 进程级 stop event 登记
    │   │   ├── turn_intake.py    # 轮次接收决策(intake_human_message)
    │   │   ├── usecase.py        # persist_ai_turn / enqueue_audit
    │   │   ├── context.py        # history 装配
    │   │   ├── context_schema.py # ChatContextSchema frozen dataclass
    │   │   ├── compression.py    # 上下文压缩
    │   │   ├── prompts.py        # chat prompt 字符串
    │   │   ├── session_policy.py # 切日规则
    │   │   ├── state.py          # MainDialogueState / AuditState TypedDict
    │   │   └── pagination.py     # cursor 编解码
    │   ├── audit/           # 审查 pipeline 域
    │   │   ├── models.py    # ORM:AuditRecord / RollingSummary
    │   │   ├── schemas.py   # Pydantic schema(含 LLM 工具描述)
    │   │   ├── context_schema.py # AuditContextSchema frozen dataclass
    │   │   ├── prompts.py   # 审查 system prompt
    │   │   ├── llm.py       # build_audit_llm(bind_tools + retry + fallback)
    │   │   ├── graph.py     # 4 节点 + 1 条件路由(replace-in-notes loop)
    │   │   ├── usecase.py   # write_audit_results
    │   │   ├── signals.py   # AuditSignalsManager 三态信号
    │   │   └── worker.py    # run_audit job function(入口在 app/worker.py)
    │   ├── notifications/   # 通知域
    │   │   ├── models.py    # ORM:Notification
    │   │   └── notify_stub.py # 通知桩
    │   └── expert/          # 日终专家域
    │       ├── models.py        # ORM:DailyReport
    │       ├── schemas.py       # Pydantic:ExpertReportSchema + 工具入参
    │       ├── context_schema.py # ExpertContextSchema frozen dataclass
    │       ├── prompts.py       # 日终专家 system prompt
    │       ├── llm.py           # build_expert_llm(bind_tools + retry + fallback)
    │       ├── graph.py         # 4 节点 + 1 条件路由(search/fetch → output loop)
    │       ├── tools.py         # search_history / fetch_by_ref 工具 handler
    │       ├── repository.py    # 只读数据源查询(search_* / fetch_*)
    │       ├── usecase.py       # write_expert_results (upsert)
    │       └── worker.py        # run_daily_reports cron job
    │
    ├── api/                 # HTTP 协议层(只做协议适配 + 编排)
    │   ├── health.py        # /health
    │   ├── auth.py          # /api/v1/auth/login, /logout
    │   ├── bind_tokens.py   # /api/v1/bind-tokens
    │   ├── children.py      # /api/v1/children CRUD
    │   └── me.py            # /api/v1/me/*
    │
    ├── scripts/             # CLI 脚本(创建父端 / 重置密码 / 画 mermaid)
    │   ├── _common.py       # cli_runtime 共用工具
    │   └── ...              # create_parent / reset_parent_password / draw_graph
    │
    └── artifacts/           # 生成的工件(mermaid)
```

**分层约束**:
- `core/*` 零业务依赖,不准 import 任何 `domain/*` / `api/*`
- `api/*` 只 import `core/*` + `domain/*/service|usecase`,不直接 ORM 查询
- `domain/*` 之间通信只能走 `schemas` + 显式事件 + `core` 基础设施
- `domain/*/graph.py` 不准 import 另一个 domain 的 graph

### 前端 (expo-router)

```
mobile/
├── app/                    # expo-router 文件路由((group) 只分组不进 URL)
│   ├── _layout.tsx         # 根布局 + 角色路由
│   ├── (auth)/             # 登录路由组
│   ├── (child)/            # 子端
│   ├── (parent)/           # 父端
│   └── (dev)/              # 开发工具
├── components/             # 共享组件(ui / layout / business / chat / icons / mascot)
├── hooks/                  # 自定义 hooks
├── stores/                 # zustand store (auth / chat)
├── services/               # API 客户端
├── lib/                    # 工具 (SSE 客户端 / streamBuffer / birthDateUtils)
├── theme/                  # 颜色 / 间距 / ThemeProvider
├── constants/              # 全局常量
└── assets/                 # 静态资源
```

## 运行环境

后端整套跑在 Docker Compose 上(API / PostgreSQL / Redis / pgAdmin / RedisInsight / worker)。本地代码以卷挂载形式进容器,**所有命令**(迁移、测试、lint、脚本)通过 `docker compose exec api ...` 在容器内执行,不绕过容器直接连库/连服务。

## 工程纪律

### DB 与 Redis 资源管理

DB engine 是全进程唯一实例,由 `RuntimeResources` 托管;lifespan 是唯一生死周期管理点。DB session 短作用域——任何 handler / 图节点显式 `async with db_session_factory() as db:`,块退出即还池;StreamingResponse 不持 DB 连接。Redis 三条连接(主 / audit / arq_pool)统一在 `redis_lifespan` 与 `build_runtime` 内创建,无业务读则不创建。写入走 staging:DB commit 由 `commit_with_redis` 包办,Redis op 由 `stage_redis_op` 挂载,顺序 = 先 DB commit 再 flush;DB 失败则 ops 丢弃,Redis flush 失败 log 不抛(缓存可 TTL 自愈)。

**反模式**:
- 模块级 `_engine` / `_session_maker`(`db.py` 必须只透传 `Base` 与 `get_db` 入口)
- `Depends(get_db)` 跨整个 `StreamingResponse`(`StreamingResponse` 异常路径不再触发 `db.close()`)
- "读路径不需要事务管理"假设(零写入路径也必须 `await db.commit()` 关闭只读事务,否则 PG 端 `idle in transaction`)
- `redis_lifespan` 内创建没人读的连接(被 `build_runtime` 托管,不要在 lifespan 重复)
- 图节点从闭包/外部拿 db session(图节点应从 `ctx.db_session_factory()` 自取短块,保持节点可移植)
- 测试 `mock_rr.db_session_factory` 用 `side_effect` 交替列表(用纯 callable 返回共享 session)
- Redis op 不走 staging,直接 `redis.setex`(DB rollback 时 Redis 写入已发生,缓存指向不存在的数据)

### 测试隔离纪律

测试入口只能走 conftest fixture,绝不绕开。守卫三道闸:库名 `_test` 断言(配置错挂的瞬间 fail)、真库行数 baseline 比对(拦住"用真库跑了"的漏网情况)、全局状态(如 LLM override)由 autouse fixture 托管生命周期——进场断言无残留、退出无条件清理。全局状态的 reset 函数**不**直接 import,测试作者只声明 fixture 参数。

**反模式**:
- 直接连真实资源:`subprocess` 跑 `app.scripts.*` / `httpx.Client(base_url="http://localhost:8000")` / `redis.Redis(host=...)` / `settings.database_url` 自建 engine
- `flushdb()` / `flushall()`(即便在 fakeredis 里也别用,绕过 fixture 控制)
- 写"测不出真东西"的不可测场景(依赖已不存在的 yield 点 / 与 fake 行为矛盾的断言 / 永久 green 但路径没走到的"防御性检查")
- 保留被移除功能的"孤儿断言"(重构时逐个测试跑 + 全量 grep 功能关键字,确保断言一并清掉)
- 一次性迁移脚本 commit 进仓库(临时脚手架迁移完成立即删,否则成为"死代码 + 误导")
- 自定义 async context manager 异常路径不 rollback(后续 `commit_with_redis` 会触发 `PendingRollbackError` 污染同测试内所有断言)
- Pydantic enum key 用字符串(应走 enum,字符串会静默失效,override 失败但测试看起来通过)
- 跨测试手动清理全局状态(用 `teardown_method` 或 try/finally,漏一个就跨测试泄漏,下一个测试的根因极难定位)

### 全栈异步架构

所有 I/O 路径走 async。依赖层 `sqlalchemy[asyncio]` + `redis.asyncio` + `arq`;测试 `asyncio_mode = "auto"`,`async def` 测试自动识别。路由 handler 全 `async def`,SSE 走 `async def -> AsyncIterator[bytes]` 生成器;LLM 调用 `async for chunk in llm.astream(messages)`,LangGraph `.astream_events(version="v2")`。业务并发原语:`asyncio.Lock` / `asyncio.Event` / `asyncio.Task`。

### 编码与数据规范

- **HTTPException 状态码**: 必须 `from fastapi import status` + 用 `status.HTTP_xxx_xxx` 常量,禁止裸数字(`status_code=...` 装饰器同样规则)
- **Python 3.14 PEP 758**: `except` / `except*` 支持不带括号的多个异常类型
- **`messages.role`**: DB 存 `human` / `ai`(不是 `user` / `assistant`),对齐 LangChain `HumanMessage` / `AIMessage`
- **中文注释**: 工程代码(domain/core/api)注释与 docstring 统一用中文 Google 风格，半角标点。测试代码可放松。
- **ORM 优先**: 简单查询走 SQLAlchemy ORM(`select()` + `.where()` + `.join()`)，禁止裸 SQL(`text()`)。本项目所有只读查询走 ORM，唯一允许 `text()` 的例外是 PG JSONB 元素展开（如 `jsonb_array_elements`，必须加注释说明）；`ON CONFLICT DO UPDATE` 已统一通过 `sqlalchemy.dialects.postgresql.insert` 表达，不需要 `text()`。

### DB 迁移纪律

- 迁移必须从 ORM 模型变更自动生成,禁止手写 DDL(autogenerate 漏点应回 ORM 修,模型先行)
- 流程:改 `app/domain/*/models.py` → `alembic revision --autogenerate -m "<描述>"` → 检查 upgrade()/downgrade() → `alembic upgrade head` → `alembic check`(应输出 `No new upgrade operations detected`)
- 基线迁移(如 `1d8a14cc596f`)可只写 upgrade;注释修正应独立 commit

## 静态检查与格式

后端用 ruff(lint + format)与 basedpyright(类型检查,仅 `app/`),配置在 `backend/pyproject.toml`。提交前在容器内跑:

```
docker compose exec api ruff format
docker compose exec api ruff check
docker compose exec api basedpyright
```
