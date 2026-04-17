# LittleBox

> 儿童 AI 对话产品，家长端 + 子端实时消息协作

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | FastAPI + SQLAlchemy (async) + Alembic + PostgreSQL + Redis |
| 前端 | Expo SDK 54 (React Native) + expo-router + zustand |
| 基础设施 | Docker Compose |

## 本地开发

### 前置要求

- Docker Desktop
- Node.js 22+
- Expo CLI (`npx expo start` 可直接运行)
- 手机 + Expo Go App（推荐），或电脑模拟器

### 1. 启动后端

```bash
cp .env.example .env
docker compose up -d
```

等待所有容器健康启动（约 10-20 秒）。

### 2. 启动前端

```bash
cd mobile
npm install
npx expo start
```

用 Expo Go 扫码或等待 Metro bundler 加载。

### 3. 登录验证

- 默认进入登录页
- 点击「模拟家长登录」→ 进入家长端，3 个 Tab 可切换
- 点击「模拟子端登录」→ 进入子端会话列表
- 点击「退出登录」→ 返回登录页

## 服务地址

| 服务 | 地址 |
|------|------|
| API | http://localhost:8000 |
| Swagger UI | http://localhost:8000/docs |
| pgAdmin | http://localhost:16050 |
| RedisInsight | http://localhost:16540 |

> Windows Hyper-V 用户注意：pgAdmin 默认 5050 端口和 RedisInsight 默认 5540 端口被 Hyper-V 保留，已改用上述端口。

## 端到端验证清单

- [ ] `docker compose up -d` 所有容器正常启动
- [ ] `http://localhost:8000/health` 返回 `{"status":"ok"}`
- [ ] `http://localhost:8000/docs` Swagger UI 可用
- [ ] pgAdmin 可登录并连接 PostgreSQL
- [ ] RedisInsight 可连接 Redis
- [ ] `docker compose exec api alembic upgrade head` 执行成功
- [ ] 后端代码修改后 uvicorn 自动 reload
- [ ] `npx expo start` 前端启动正常
- [ ] 模拟登录 → 角色路由切换正常
- [ ] 家长端 3 Tab 切换正常
- [ ] 子端会话列表 → 聊天页面跳转正常

## 项目结构

```
LittleBox/
├── backend/              # FastAPI 后端
│   ├── app/
│   │   ├── main.py       # 应用入口
│   │   ├── config.py     # pydantic-settings 配置
│   │   └── api/          # 路由模块
│   ├── alembic/           # 数据库迁移
│   └── Dockerfile
├── mobile/               # Expo RN 前端
│   ├── app/              # expo-router 页面
│   │   ├── (auth)/       # 登录路由组
│   │   ├── (child)/      # 子端路由组
│   │   └── (parent)/     # 家长端路由组（Tab 导航）
│   ├── stores/           # zustand 状态管理
│   └── components/       # 共享组件
├── docker-compose.yml
└── .env.example
```

## 常用命令

### 后端

```bash
# 进入容器执行命令
docker compose exec api <command>

# 运行迁移
docker compose exec api alembic revision --autogenerate -m "message"
docker compose exec api alembic upgrade head

# 代码检查
docker compose exec api ruff check .
docker compose exec api mypy app
```

### 前端

```bash
cd mobile
npx expo start --clear     # 清除缓存启动
```
