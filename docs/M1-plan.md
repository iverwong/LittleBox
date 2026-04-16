# M1 · 项目骨架与基础设施 — 实施计划 (1/17)

## 目标概述

从零搭建 LittleBox 项目的完整开发骨架：后端 FastAPI 项目结构 + 前端 Expo RN 项目结构 + Docker Compose 本地开发环境，验证一键启动可用。本计划不涉及业务逻辑和数据建模（M2 范围）。

---

## 技术版本基线

| 技术栈 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.14.4 | 最新稳定版，2026-04-07 发布 |
| FastAPI | 最新 pip 版本 | async 框架，SSE streaming 支持 |
| SQLAlchemy | 最新 pip 版本 | async 模式 + asyncpg 驱动 |
| Alembic | 最新 pip 版本 | 数据库迁移，适配 async engine |
| PostgreSQL | 18.3 | Docker 官方镜像 postgres:18 |
| Redis | 8.6 | Docker 官方镜像 redis:8.6 |
| pgAdmin | 最新 | Docker 镜像 dpage/pgadmin4 |
| RedisInsight | 最新 | Docker 镜像 redis/redisinsight |
| Expo SDK | 55 | React Native 0.83 + React 19.2 |
| TypeScript | 最新 | 前端语言 |
| expo-router | 随 SDK 55 | 文件路由，Expo 标配导航方案 |
| zustand | 最新 | 轻量状态管理 |

---

## 前端架构决策

### 导航方案：expo-router（文件路由）

选择理由：

- Expo 55 官方推荐，与 Expo 生态深度集成
- 文件系统即路由，结构直观
- 内置 deep linking、类型安全路由

### 状态管理：zustand

选择理由：

- 极简 API，无 Provider 包裹，无 action/reducer 样板
- 当前业务复杂度（auth 状态 + 聊天消息 + 配置）完全够用
- 后续如需扩展，可无缝引入 middleware（persist、immer 等）

### 导航结构

```
app/
├── _layout.tsx              # Root layout（加载字体、初始化等）
├── (auth)/                  # 未登录路由组
│   ├── _layout.tsx
│   ├── login.tsx            # 家长登录（手机号 + 验证码）
│   └── scan.tsx             # 子端扫码登录
├── (child)/                 # 子账号路由组
│   ├── _layout.tsx          # Stack Navigator
│   ├── index.tsx            # 会话列表
│   └── chat/[sessionId].tsx # 聊天界面
└── (parent)/                # 家长路由组
    ├── _layout.tsx          # Tab Navigator（3 tabs）
    ├── (children)/          # Tab 1: 孩子管理
    │   ├── _layout.tsx      # Stack
    │   └── index.tsx
    ├── (notifications)/     # Tab 2: 通知中心
    │   ├── _layout.tsx      # Stack
    │   └── index.tsx
    └── (settings)/          # Tab 3: 设置
        ├── _layout.tsx      # Stack
        └── index.tsx
```

- 登录后根据用户角色（parent/child）路由到对应路由组
- 子端极简：只有会话列表和聊天两个页面
- 父端三 Tab，每个 Tab 内嵌 Stack 支持后续页面层级扩展

---

## 执行步骤

### Step 1：GitHub 仓库初始化

创建 `LittleBox` 仓库，初始化基础文件：

- [ ]  在 GitHub 创建公开仓库 `LittleBox`
- [ ]  初始化 `.gitignore`（Python + Node + macOS + IDE）
- [ ]  创建 `README.md`（项目简介、技术栈、本地开发启动说明占位）
- [x]  创建 `LICENSE`（暂不添加）
- [ ]  确定顶层目录结构：

```
LittleBox/
├── backend/          # Python FastAPI 后端
├── mobile/           # Expo RN 前端
├── docker-compose.yml
├── .env.example      # 环境变量模板
├── README.md
└── .gitignore
```

**验证**：仓库可 clone，目录结构符合预期

**提交**：`chore: init repository structure`

---

### Step 2：后端项目结构 + 依赖配置

- [ ]  创建 `backend/pyproject.toml`，声明项目元数据和依赖
- [ ]  创建后端目录骨架（空 `__init__.py` + 模块占位）
- [ ]  创建 `backend/Dockerfile`（Python 3.14 基础镜像，pip install）

**`backend/pyproject.toml` 核心依赖**：

```toml
[project]
name = "littlebox-backend"
version = "0.0.1"
requires-python = ">=3.14"
dependencies = [
    "fastapi[standard]",
    "uvicorn[standard]",
    "sqlalchemy[asyncio]",
    "asyncpg",
    "alembic",
    "redis[hiredis]",
    "pydantic-settings",
]

[project.optional-dependencies]
dev = [
    "pytest",
    "pytest-asyncio",
    "httpx",          # FastAPI 测试客户端
    "ruff",           # linter + formatter
    "mypy",
]
```

**后端目录骨架**：

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI 应用入口
│   ├── config.py         # pydantic-settings 配置
│   ├── api/              # 路由模块
│   │   ├── __init__.py
│   │   └── health.py     # /health 端点
│   ├── chat/             # 主对话链路（占位）
│   │   └── __init__.py
│   ├── audit/            # 审查 Pipeline（占位）
│   │   └── __init__.py
│   ├── expert/           # 日终专家 Agent（占位）
│   │   └── __init__.py
│   ├── notify/           # 推送通知（占位）
│   │   └── __init__.py
│   ├── models/           # SQLAlchemy ORM（占位）
│   │   └── __init__.py
│   └── state/            # Redis 会话状态（占位）
│       └── __init__.py
├── alembic/              # Step 4 创建
├── alembic.ini           # Step 4 创建
├── pyproject.toml
├── Dockerfile
└── requirements.txt      # pip freeze 产物，Docker 构建用
```

**Dockerfile 要点**：

- 基础镜像 `python:3.14-slim`
- 工作目录 `/app`
- `pip install -r requirements.txt`
- `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]`
- 开发模式通过 docker-compose 覆盖 CMD 加 `--reload`

**验证**：`pip install -e ".[dev]"` 成功，import 各模块无报错

**提交**：`chore: scaffold backend project structure and dependencies`

---

### Step 3：FastAPI 应用入口

- [ ]  实现 `app/config.py` — 基于 `pydantic-settings` 的配置管理
- [ ]  实现 `app/main.py` — FastAPI 应用工厂
- [ ]  实现 `app/api/health.py` — 健康检查端点

**`app/config.py`**：

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """应用配置，从环境变量读取。"""
    # 数据库
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/littlebox"
    # Redis
    redis_url: str = "redis://localhost:6379/0"
    # 应用
    app_name: str = "LittleBox"
    debug: bool = False
    # CORS
    cors_origins: list[str] = ["*"]  # TODO 开发阶段允许所有来源

    model_config = {"env_prefix": "LB_", "env_file": ".env"}

settings = Settings()
```

**`app/main.py`**：

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.api.health import router as health_router

def create_app() -> FastAPI:
    """应用工厂。"""
    application = FastAPI(
        title=settings.app_name,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(health_router)
    return application

app = create_app()
```

**`app/api/health.py`**：

```python
from fastapi import APIRouter

router = APIRouter(tags=["health"])

@router.get("/health")
async def health_check():
    """健康检查端点，Docker healthcheck 和外部监控使用。"""
    return {"status": "ok"}
```

**验证**：`uvicorn app.main:app --reload`，访问 `http://localhost:8000/health` 返回 `{"status": "ok"}`

**提交**：`feat: add FastAPI app entry with health check and CORS`

---

### Step 4：Alembic 数据库迁移配置

- [ ]  初始化 Alembic：`alembic init alembic`
- [ ]  修改 `alembic.ini`：`sqlalchemy.url` 指向 PostgreSQL
- [ ]  修改 `alembic/env.py`：适配 async SQLAlchemy engine
- [ ]  创建 `app/models/base.py`：声明 `DeclarativeBase`

**`app/models/base.py`**：

```python
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    """所有 ORM 模型的基类。M2 中各模块模型继承此类。"""
    pass
```

**`alembic/env.py` 关键改动**：

```python
from sqlalchemy.ext.asyncio import async_engine_from_config
import asyncio
from app.models.base import Base
from app.config import settings

# target_metadata 指向 Base.metadata
target_metadata = Base.metadata

def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations():
    connectable = async_engine_from_config(
        {"sqlalchemy.url": settings.database_url},
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()

def run_migrations_online():
    asyncio.run(run_async_migrations())
```

**验证**：

- PostgreSQL 容器启动后（Step 5），运行 `alembic revision --autogenerate -m "init"` 能生成空迁移
- `alembic upgrade head` 执行成功

**提交**：`chore: configure Alembic with async SQLAlchemy`

---

### Step 5：Docker Compose 编排

- [ ]  创建 `docker-compose.yml`：5 个服务
- [ ]  创建 `.env.example`：环境变量模板
- [ ]  创建 `backend/.dockerignore`

**`docker-compose.yml`**：

```yaml
services:
  api:
    build: ./backend
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    volumes:
      - ./backend:/app          # hot-reload: 源码挂载
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 3

  db:
    image: postgres:18
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-postgres}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
      POSTGRES_DB: ${POSTGRES_DB:-littlebox}
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 3s
      retries: 5

  redis:
    image: redis:8.6
    ports:
      - "6379:6379"
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  pgadmin:
    image: dpage/pgadmin4
    environment:
      PGADMIN_DEFAULT_EMAIL: ${PGADMIN_EMAIL:-admin@littlebox.dev}
      PGADMIN_DEFAULT_PASSWORD: ${PGADMIN_PASSWORD:-admin}
      PGADMIN_LISTEN_PORT: 5050
    ports:
      - "5050:5050"
    depends_on:
      - db

  redisinsight:
    image: redis/redisinsight:latest
    ports:
      - "5540:5540"
    depends_on:
      - redis

volumes:
  pgdata:
  redisdata:
```

**`.env.example`**：

```bash
# === 后端 ===
LB_DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/littlebox
LB_REDIS_URL=redis://redis:6379/0
LB_DEBUG=true

# === PostgreSQL ===
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=littlebox

# === pgAdmin ===
PGADMIN_EMAIL=admin@littlebox.dev
PGADMIN_PASSWORD=admin
```

**验证**：

- `cp .env.example .env && docker compose up -d`
- 所有 5 个容器健康运行
- `http://localhost:8000/health` → `{"status": "ok"}`
- `http://localhost:8000/docs` → Swagger UI 可访问
- `http://localhost:5050` → pgAdmin 可登录
- `http://localhost:5540` → RedisInsight 可访问
- 修改 `app/api/health.py` 后 uvicorn 自动 reload ✅

**提交**：`feat: add Docker Compose with PostgreSQL, Redis, pgAdmin, RedisInsight`

---

### Step 6：前端 Expo 项目初始化

- [ ]  在 `mobile/` 目录初始化 Expo 项目
- [ ]  配置 TypeScript
- [ ]  安装核心依赖

**初始化命令**：

```bash
cd mobile
npx create-expo-app@latest . --template tabs
```

> `tabs` 模板自带 expo-router + TypeScript 配置，省去手动配置成本。初始化后清理模板自带的示例页面。
> 

**安装额外依赖**：

```bash
npx expo install zustand
npx expo install expo-secure-store    # 后续存储 auth token
```

**验证**：`npx expo start` 能启动开发服务器，模拟器/Expo Go 可加载

**提交**：`chore: init Expo RN project with TypeScript`

---

### Step 7：前端导航骨架

- [ ]  清理模板示例页面
- [ ]  按导航结构创建路由文件
- [ ]  实现根 layout（占位：模拟角色切换）
- [ ]  实现 auth 状态 store

**创建文件结构**：

```
mobile/app/
├── _layout.tsx              # Root layout：读取 auth 状态，路由到对应组
├── (auth)/
│   ├── _layout.tsx          # Stack layout
│   ├── login.tsx            # 家长登录占位页
│   └── scan.tsx             # 子端扫码占位页
├── (child)/
│   ├── _layout.tsx          # Stack layout
│   ├── index.tsx            # 会话列表占位页
│   └── chat/
│       └── [sessionId].tsx  # 聊天界面占位页
└── (parent)/
    ├── _layout.tsx          # Tab layout（3 tabs）
    ├── (children)/
    │   ├── _layout.tsx
    │   └── index.tsx        # 孩子管理占位页
    ├── (notifications)/
    │   ├── _layout.tsx
    │   └── index.tsx        # 通知中心占位页
    └── (settings)/
        ├── _layout.tsx
        └── index.tsx        # 设置占位页
```

**`mobile/stores/auth.ts`**（zustand store）：

```tsx
import { create } from 'zustand'

type Role = 'parent' | 'child' | null

interface AuthState {
  role: Role
  token: string | null
  setAuth: (role: Role, token: string) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  role: null,
  token: null,
  setAuth: (role, token) => set({ role, token }),
  logout: () => set({ role: null, token: null }),
}))
```

**Root `_layout.tsx` 逻辑**：

- 读取 `useAuthStore` 的 `role`
- `role === null` → 渲染 `(auth)` 路由组
- `role === 'child'` → 重定向到 `(child)`
- `role === 'parent'` → 重定向到 `(parent)`

**占位页面**：每个页面只需显示页面名称和角色信息，如「[子端] 会话列表」，M1 不实现任何业务逻辑。

**验证**：

- 启动 App，默认进入登录页
- 登录占位页有两个按钮：「模拟家长登录」/「模拟子端登录」
- 点击后切换到对应角色的页面组
- 家长端 3 个 Tab 可切换
- 子端会话列表 → 点击进入聊天页面 → 可返回

**提交**：`feat: add navigation skeleton with role-based routing`

---

### Step 8：本地开发环境一键启动验证

- [ ]  更新 `README.md`：完整的本地开发启动说明
- [ ]  验证全流程：clone → 启动 → 可用

[**README.md](http://README.md) 应包含**：

1. 项目简介
2. 技术栈概览
3. 前置要求（Docker、Node.js、Expo CLI）
4. 后端启动：`cp .env.example .env && docker compose up -d`
5. 前端启动：`cd mobile && npm install && npx expo start`
6. 服务地址一览表（API / pgAdmin / RedisInsight）
7. 项目目录结构说明

**端到端验证清单**：

- [ ]  `docker compose up -d` 所有容器正常启动
- [ ]  `http://localhost:8000/health` 返回 ok
- [ ]  `http://localhost:8000/docs` Swagger UI 可用
- [ ]  `http://localhost:5050` pgAdmin 可登录并连接 PostgreSQL
- [ ]  `http://localhost:5540` RedisInsight 可连接 Redis
- [ ]  `alembic upgrade head` 执行成功（空迁移）
- [ ]  后端代码修改后 uvicorn 自动 reload
- [ ]  `npx expo start` 前端启动正常
- [ ]  模拟登录 → 角色路由切换正常
- [ ]  家长端 3 Tab 切换正常
- [ ]  子端会话列表 → 聊天页面跳转正常

**提交**：`docs: add development setup guide to README`

**最终提交**：合并到 main 分支，M1 完成 ✅