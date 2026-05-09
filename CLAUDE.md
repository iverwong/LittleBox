# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LittleBox is a parent-child AI chat application. Parents monitor children via an AI chat interface, with AI-powered safety auditing and daily reporting. The child runs a minimal chat client; the parent has a management dashboard.

**Current milestone: M4.8** (account backend completion). The project follows a 17-milestone plan documented in `docs/M1-plan.md` through `docs/M4.8-plan.md`.

## Development Commands

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
docker compose exec api basedpyright app

# Run a single test
docker compose exec api pytest tests/path/to/test_file.py::test_name -v

# CLI scripts (M4 auth)
docker compose exec api python -m app.scripts.create_parent --note "..."
docker compose exec api python -m app.scripts.reset_parent_password --phone <4-letter-id>
```

### Frontend (Expo React Native)

```bash
cd mobile

# Install deps
npm install

# Start dev server
npx expo start

# Android emulator
npx expo start --android

# iOS simulator
npx expo start --ios
```

### Infrastructure (Docker)

```bash
# Start all services (PostgreSQL, Redis, pgAdmin, RedisInsight, API)
docker compose up -d

# Rebuild API image
docker compose build api

# View logs
docker compose logs -f api
```

### Environment Setup

Copy `.env.example` to `.env` and fill in:
- `LB_DASHSCOPE_API_KEY` / `DASHSCOPE_API_KEY`: DashScope API key (required for live LLM tests)
- `LB_DATABASE_URL`: PostgreSQL connection string
- `LB_REDIS_URL`: Redis connection string

## Architecture

### Backend Structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app factory
│   ├── config.py            # pydantic-settings (env var prefix: LB_)
│   ├── api/
│   │   ├── health.py        # GET /health
│   │   ├── auth.py          # POST /api/v1/auth/login, /api/v1/auth/logout
│   │   ├── bind_tokens.py   # POST /api/v1/bind-tokens, GET /api/v1/bind-tokens/{bind_token}/status, POST /api/v1/bind-tokens/{bind_token}/redeem
│   │   ├── children.py      # Parent: list/add children, generate bind QR
│   │   ├── me.py            # GET /api/v1/me, GET /api/v1/me/profile
│   │   └── dev_chat.py      # ⚠️ TEMPORARY M3 demo route (deleted at M7)
│   ├── schemas/
│   │   └── accounts.py      # Pydantic request/response schemas for accounts
│   ├── models/
│   │   ├── enums.py        # UserRole, Gender enums
│   │   ├── accounts.py     # users, auth_tokens, child_profiles, family_members
│   │   ├── chat.py         # sessions, messages
│   │   ├── audit.py        # audit_records, rolling_summaries
│   │   └── parent.py       # daily_reports, notifications, data_deletion_requests
│   ├── chat/               # LLM streaming (M3)
│   ├── auth/               # Password hashing (argon2), token ops
│   ├── audit/              # Audit pipeline (placeholder, M8)
│   ├── expert/             # Daily expert agent (placeholder, M12)
│   ├── notify/             # Push notifications (placeholder, M9)
│   └── state/              # Redis session state (placeholder, M5)
├── alembic/                 # DB migrations (async SQLAlchemy)
└── tests/
```

### Frontend Structure

```
mobile/
├── app/                    # expo-router file-based routing
│   ├── _layout.tsx         # Root layout with role-based routing
│   ├── index.tsx           # Entry point
│   ├── +html.tsx           # HTML fallback
│   ├── +not-found.tsx      # 404 handler
│   ├── dev-chat.tsx        # ⚠️ TEMPORARY M3 demo page (deleted at M7)
│   ├── (auth)/             # Login routes (group - no URL prefix)
│   │   ├── _layout.tsx
│   │   ├── login.tsx
│   │   └── scan.tsx        # QR scan for child bind
│   ├── (child)/            # Child client (group - no URL prefix)
│   │   ├── _layout.tsx
│   │   ├── index.tsx       # Session list
│   │   └── chat/[sessionId].tsx
│   ├── (parent)/           # Parent dashboard (group - no URL prefix)
│   │   ├── _layout.tsx
│   │   ├── children/
│   │   ├── notifications/
│   │   └── settings/
│   └── (dev)/              # Dev tools (group - no URL prefix)
│       ├── _layout.tsx
│       └── components.tsx
├── stores/auth.ts          # zustand auth store
└── lib/sseClient.ts        # SSE client wrapper (react-native-sse)
```

Note: `(group)` directories are expo-router groups — they group routes without adding to the URL path.

### Auth Architecture (M4 Decision)

- **Opaque tokens**: 32-byte `secrets.token_urlsafe(32)`, stored as sha256 hash in DB, cached in Redis (`auth:{sha256(token)}` → JSON payload, TTL 10min rolling)
- **Parent token**: 7-day rolling, renewed on each request if last_rolled_date != today (Asia/Shanghai); child tokens never expire
- **Child bind flow**: Parent generates `bind_token` (16-byte, 5min TTL in Redis) → QR code → child scans via `POST /api/v1/bind-tokens/{bind_token}/redeem`
- **Auth depends**: `get_current_account` → `require_parent` / `require_child` role guards
- **Device binding**: All tokens bound to `X-Device-Id` header; mismatches trigger immediate revocation

### Streaming Architecture (M3, stable)

1. Client sends message → FastAPI `POST /api/dev/chat/stream`
2. `stream_chat()` generator runs LangGraph `.astream_events(version="v2")`
3. `on_chat_model_stream` events → SSE `delta` frames
4. SSE protocol: `{"type": "start"|"delta"|"end"|"error", ...}` (JSON in `data:`)

### Key LLM Decisions (from M3-plan.md)

- **Model**: `qwen3.5-flash` via DashScope SDK (NOT百炼兼容端)
- **思考模式关闭**: `enable_thinking=False` (M3 validation milestone)
- **流式接口**: `AioMultiModalConversation.call(stream=True, incremental_output=True)` — 多模态接口，纯文本也必须包 `content=[{"text": "..."}]`
- **usage_metadata**: 仅在末条 chunk 透传，避免 token 计数累加翻倍
- **重试禁用**: `max_retries=0` (重试破坏流式语义)

### Database Schema (12 tables, M2)

- **accounts**: families, users, child_profiles, auth_tokens, device_tokens
- **chat**: sessions, messages (role = human/ai, 对齐 LangChain)
- **audit**: audit_records, rolling_summaries
- **parent**: daily_reports, notifications, data_deletion_requests

Indexes: `(session_id, created_at)` on messages, `(session_id, turn_number)` on audit_records, `(child_user_id, status)` on sessions, `(child_user_id, report_date)` on daily_reports.

### SSE Event Protocol

```json
{"type": "start",  "session_id": "<uuid>"}
{"type": "delta",  "content": "你"}
{"type": "end",    "finish_reason": "stop"}
{"type": "error",  "message": "...", "code": "UpstreamError"}
```

Client disconnect: FastAPI generator receives `asyncio.CancelledError` → Starlette cleans up httpx connection. No manual `is_disconnected()` polling needed.

## Temporary Code Cleanup Contract (M7)

The following files are M3/M4 temporary artifacts scheduled for deletion at M7:
- `backend/app/api/dev_chat.py` — dev streaming endpoint
- `backend/app/chat/llm.py` — KEEP (becomes M6+ LLM module)
- `backend/app/chat/graph.py` — KEEP (expand to full main chat graph)
- `backend/app/chat/sse.py` — KEEP (protocol already stable)
- `mobile/app/dev-chat.tsx` — dev demo page

## Critical Gotchas

### Auth (M4)
- **`X-Device-Id` header required**: Every `/auth/*` and protected endpoint must send `X-Device-Id` header; missing → 422.
- **Redis lifespan**: `auth.redis` is initialized in `main.py` lifespan; do not create Redis clients per-request.
- **argon2 verification**: `VerifyMismatchError` must be caught separately — other exceptions (`VerifyError`, `InvalidHashError`) indicate internal failures, return 500.
- **Partial unique index**: `users.phone` has `WHERE role='parent' AND is_active=true` partial index; alembic autogenerate may try to drop it — always review migration files.

### Streaming (M3)
- **`dashscope` SDK** requires `DASHSCOPE_API_KEY` env var (NOT `LB_DASHSCOPE_API_KEY`). The `LB_DASHSCOPE_API_KEY` is used by `app.config.py`, but the SDK itself reads `DASHSCOPE_API_KEY` directly.
- **LangGraph `disable_streaming`**: Must remain `False`. If set to `True`, `.astream_events()` stops emitting `on_chat_model_stream` events.
- **`get_chat_llm()`**: Uses `@lru_cache(maxsize=1)`. In tests, call `.cache_clear()` before patching to avoid stale singleton.
- **Patch location for `get_chat_llm`**: Patch `app.chat.graph.get_chat_llm` (usage site), not `app.chat.llm.get_chat_llm` (definition site) — Python import binding.

### General
- **Python 3.14**: Test files must use `pytest.mark.asyncio` and `async def` functions.
- **`messages.role`**: DB stores `human`/`ai` (not `user`/`assistant`), aligned with LangChain `HumanMessage`/`AIMessage`.
- **pgAdmin port**: Mapped to `16050:5050` (not `5050:5050`) to avoid conflict.
- **RedisInsight port**: Mapped to `16540:5540` (not `5540:5540`).

### Test Isolation Discipline (M6-patch)
- **所有涉及 DB / Redis 的测试必须通过 conftest fixture 进入**：
  - DB: `db_session` (savepoint rollback, function scope)
  - HTTP: `api_client` (ASGI in-process, `dependency_overrides`)
  - Redis: `redis_client` (fakeredis, function scope)
- **黑名单（禁止使用）**：
  - `subprocess` / `Popen` 跑 `app.scripts.*` 连真实库
  - `httpx.Client(base_url="http://localhost:8000")` 直连真 server
  - `redis.Redis(host="redis", ...)` 显式连真实 host
  - `from app.config import settings` 后用 `settings.database_url` 自建 engine
  - `flushdb()` / `flushall()`
- **双层运行时防御**（`backend/tests/conftest.py`）：
  - 模块级 `_test_url()` 断言：数据库名必须含 `_test`，否则 pytest 立即 abort
  - session autouse `_prod_db_row_count_guard`：记录真库行数 baseline，session 结束比对，检测到变化即 fail
- **历史教训**：M6-patch · 测试隔离纪律加固（`docs/M6-patch.md`）
