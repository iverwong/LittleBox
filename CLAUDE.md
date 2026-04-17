# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LittleBox is a children's AI dialogue product with parent + child real-time messaging. Architecture: FastAPI backend (Python 3.14) + Expo RN frontend (SDK 55) + PostgreSQL + Redis.

## Common Commands

### Backend (always via Docker)

```bash
# Run backend commands inside container
docker compose exec api <command>

# Examples:
docker compose exec api alembic revision --autogenerate -m "message"
docker compose exec api alembic upgrade head
docker compose exec api pytest
docker compose exec api ruff check .
docker compose exec api ruff format .
docker compose exec api mypy app

# Run a single test
docker compose exec api pytest tests/path/to/test_file.py::test_name -v
```

### Frontend

```bash
cd mobile
npm install
npx expo start
# Platform-specific
npx expo start --ios
npx expo start --android
```

### Docker Compose

```bash
docker compose up -d          # Start all services
docker compose down           # Stop all services
docker compose ps            # Check status
docker compose logs -f api    # Follow API logs
```

## Architecture

### Backend Structure

```
backend/
├── app/
│   ├── main.py           # FastAPI application factory (create_app())
│   ├── config.py         # pydantic-settings (LB_* env prefix)
│   ├── api/              # Route modules (health.py)
│   ├── chat/              # Chat pipeline (placeholder)
│   ├── audit/             # Content audit pipeline (placeholder)
│   ├── expert/            # Daily expert agent (placeholder)
│   ├── notify/            # Push notifications (placeholder)
│   ├── models/            # SQLAlchemy ORM models (placeholder)
│   └── state/             # Redis session state (placeholder)
├── alembic/               # Database migrations (async engine)
├── alembic.ini
├── pyproject.toml
└── Dockerfile
```

- `create_app()` is the application factory in `main.py`
- Settings use `LB_` env prefix via pydantic-settings
- PostgreSQL via async SQLAlchemy (asyncpg driver)

### Frontend Structure (expo-router file-based routing)

```
mobile/
├── app/
│   ├── _layout.tsx        # Root layout with theme + navigation
│   ├── (tabs)/            # Tab navigation (index.tsx, two.tsx)
│   ├── +html.tsx          # HTML fallback
│   ├── +not-found.tsx     # 404 handler
│   └── modal.tsx          # Modal presentation
├── components/             # Shared components
├── assets/                 # Fonts, images
└── package.json
```

- State management: zustand
- Navigation: expo-router + react-navigation (Stack)
- Theming: @react-navigation/native theming with DarkTheme/DefaultTheme

### Service Network

| Service | Internal | Host |
|---------|----------|------|
| PostgreSQL | db:5432 | localhost:5432 |
| Redis | redis:6379 | localhost:6379 |
| API | api:8000 | localhost:8000 |
| pgAdmin | - | localhost:5050 |
| RedisInsight | - | localhost:5540 |

## Environment Variables

- Backend config uses `LB_` prefix via pydantic-settings
- Template: `.env.example`
- Database URL in Docker: `postgresql+asyncpg://postgres:postgres@db:5432/littlebox`

## Git Conventions

Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`

## Implementation Workflow

When user provides an implementation plan, follow this iterative confirmation process:

### Step-by-Step Execution

1. **Execute one step at a time** - Implement only the current step from the plan
2. **Self-verify** - Run relevant tests/linting/type-checking before reporting completion
3. **Report for confirmation** - Present findings to user for approval before proceeding

### Confirmation Report Template

After each step, report:

```
## Step N: [Step Title]

### 1. 实施差异报告 (Implementation Delta Report)
- **与计划的差异**: [具体差异描述]
- **原因**: [差异产生的原因]
- **解决方案**: [如何处理这些差异]

### 2. 验证点报告 (Verification Report)
- **已自行验证**:
  - [验证项1]: [结果]
  - [验证项2]: [结果]
- **需用户验证**:
  - [验证项1]: [需要用户确认的内容]
  - [验证项2]: [需要用户确认的内容]

### 3. 下一步计划 (Next Steps)
[简要说明下一步要做什么]
```

### Approval Gate

Do NOT proceed to next step until user confirms approval of current step's confirmation report.