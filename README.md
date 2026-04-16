# LittleBox

> 家长端 + 子端实时消息协作应用

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | FastAPI + SQLAlchemy (async) + Alembic + PostgreSQL + Redis |
| 前端 | Expo SDK 55 (React Native 0.83) + expo-router + zustand |
| 基础设施 | Docker Compose |

## 本地开发

### 前置要求

- Docker Desktop
- Node.js 22+
- Expo CLI (`npm install -g expo-cli`)
- Python 3.14+

### 启动后端

```bash
cp .env.example .env
docker compose up -d
```

### 启动前端

```bash
cd mobile
npm install
npx expo start
```

### 服务地址

| 服务 | 地址 |
|------|------|
| API | http://localhost:8000 |
| Swagger UI | http://localhost:8000/docs |
| pgAdmin | http://localhost:5050 |
| RedisInsight | http://localhost:5540 |

## 项目结构

```
LittleBox/
├── backend/          # FastAPI 后端
├── mobile/           # Expo RN 前端
├── docker-compose.yml
└── .env.example
```
