# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LittleBox is a children's AI dialogue product with parent + child real-time messaging. Architecture: FastAPI backend (Python 3.14) + Expo RN frontend (SDK 55).

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
```

### Frontend

```bash
cd mobile
npx expo start
```

### Docker Compose

```bash
docker compose up -d          # Start all services
docker compose down           # Stop all services
docker compose ps            # Check status
```

## Architecture

### Backend Structure

```
backend/
├── app/
│   ├── main.py           # FastAPI application factory (create_app())
│   ├── config.py         # pydantic-settings (LB_* env prefix)
│   ├── api/              # Route modules (health.py, etc.)
│   ├── chat/             # Main chat pipeline
│   ├── audit/            # Content audit pipeline
│   ├── expert/           # Daily expert agent
│   ├── notify/           # Push notifications
│   ├── models/           # SQLAlchemy ORM models (Base from models/base.py)
│   └── state/            # Redis session state
├── alembic/              # Database migrations (async engine)
├── alembic.ini
├── pyproject.toml
└── Dockerfile
```

### Frontend Navigation (expo-router file-based routing)

```
mobile/app/
├── _layout.tsx           # Root layout: reads auth store, redirects by role
├── (auth)/               # Unauthenticated routes
├── (child)/              # Child mode: session list → chat
└── (parent)/             # Parent mode: 3 tabs (children/notifications/settings)
```

### Service Network (Docker Compose)

- PostgreSQL: `db:5432` (exposed as `localhost:5432`)
- Redis: `redis:6379` (exposed as `localhost:6379`)
- API: `localhost:8000`
- pgAdmin: `localhost:5050`
- RedisInsight: `localhost:5540`

## Environment Variables

- Backend config uses `LB_` prefix via pydantic-settings
- Template: `.env.example`
- Database URL in Docker: `postgresql+asyncpg://postgres:postgres@db:5432/littlebox`

## Git Conventions

Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`
