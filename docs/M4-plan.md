# M4 · 账号体系 — 实施计划 (4/17)

## 目标概述

打通 LittleBox 的账号与鉴权基座：父账号登录（**用户名 + 密码，MVP 特供**，字段名直接用 `phone` 以便 PNVS 上线 0 schema 变更）、子账号二维码扫码绑定（方案 A）、Opaque Token 签发 / 缓存 / 吊销、FastAPI 鉴权中间件、运维 CLI（生成 / 重置父账号）。Schema 同时预留 `family_members` 关联表承接「一家 N 父」扩展，不在 MVP 暴露。

> 📚 **决策背景 · 避坑记录 · 设计权衡**：详见 [M4 · 决策背景与避坑记录](https://www.notion.so/M4-7d37f77eaeeb49b388c96dc72140beac?pvs=21)。本页只留执行性内容。
> 

> 🏗️ **计划定位**：本计划为基础设施扩展，由 password / tokens / deps / api / scripts 多个并行模块组成，无统一「入口函数」，各组件独立可测试、独立验证（Agent 指引 §4.8 允许条款；详见 [M4 · 决策背景与避坑记录](https://www.notion.so/M4-7d37f77eaeeb49b388c96dc72140beac?pvs=21)）。
> 

**不做**：阿里云 PNVS 短信认证 / 一键登录（确定切 PNVS 时再做，走数据回填，0 schema 变更）；App 内密码找回（走运维 CLI 重置）；改绑手机号 / 多设备挤下线（已入 [](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)，P1 上线前）；家庭多父邀请码流程（已入清单，P2 上线后）；前端 UI（M5 接手）；家长配置 / 推送 Token 管理（M10 / M11）。

---

## 前置条件 / 风格基线

### 已有（M1-M3 沉淀）

- **后端**：FastAPI + SQLAlchemy 2.x async + asyncpg + Alembic + Redis + pydantic-settings，全部跑在 `docker compose exec api`。
- **ORM**：`app/models/` 下已建 accounts / chat / audit / parent 四大模块（12 表），`BaseMixin`（id + created_at）落定。`users` 表已有 `role` / `phone VARCHAR(20) nullable` / `is_active` / `consent_at` / `consent_version`；`auth_tokens` 表已有 `user_id` / `token_hash` / `expires_at` / `revoked_at` / `device_id`。M4 在其上**加字段 + 加索引 + 加新表**，不重建。
- **命令链**：`docker compose exec api alembic revision --autogenerate -m "..."` / `alembic upgrade head` / `pytest` / `ruff check .` / `basedpyright app`。
- **测试**：`pytest` + `pytest-asyncio` + `httpx.ASGITransport`，`markers = ["live: needs real LLM API"]`；`@lru_cache` 单例在测试中需 `.cache_clear()` + patch **使用点**（M3 教训）。
- **配置**：`pydantic-settings` 读 `.env`，所有变量 `LB_` 前缀，敏感值走 `SecretStr`。
- **时区**：所有时间戳 `TIMESTAMPTZ` 存 UTC；业务调度 `Asia/Shanghai`。
- **CORS**：`main.py` 已挂 `CORSMiddleware`，M4 不动。

### 本里程碑新增

- 新依赖：`argon2-cffi`（密码哈希，OWASP 2024 推荐，cffi 实现避开 GIL 问题）。
- 新 Redis key 命名空间：`auth:{sha256(token)}` → `{"user_id":..., "role":..., "expires_at":...}` JSON；子账号 bind_token `bind:{bind_token}` → `{"parent_user_id":..., "child_user_id":...}`，TTL 5 分钟。
- 新分支：`m4/account-system`。

---

## 技术决策基线

| 决策项 | 结论 | 说明 |
| --- | --- | --- |
| MVP 登录凭证 | `phone`（4 位小写字母）+ `password`（8 位小写字母） | 字符集 `abcdefghjkmnpqrstuvwxyz`（23 字母，去 `i/l/o`）。phone 字段名固定，PNVS 上线后直接塞真手机号，0 schema 变更；password_hash 保留半年灰度期后 drop。App 内不做密码找回 |
| 密码哈希 | argon2id，t=3 / m=64MB / p=4 | OWASP 2024 推荐参数；`argon2-cffi` 官方库。验证 `verify()` 内置时间恒定，防时序攻击 |
| phone 唯一性 | Partial unique index：`WHERE role='parent' AND is_active=true` | `users.phone` 保持 nullable（child 无 phone）；部分唯一索引保证 parent 的 phone 不重复，且被禁用的账号不占用号段 |
| Token 格式 | Opaque（32 字节 `secrets.token_urlsafe(32)`） | 不用 JWT：可主动吊销、可缓存、不泄漏载荷；`auth_tokens.token_hash` 存 sha256 而非明文；Redis `auth:{sha256(token)}` 做一级缓存（TTL 10 分钟滚动） |
| 父 Token 有效期 | 7 天滚动，每日首次请求续期 | 首次签发 `expires_at = now + 7d`。每次 resolve 命中后判断 Redis payload 里的 `last_rolled_date`：非今天（北京时间）则 DB `UPDATE expires_at = now + 7d`  • 刷 Redis payload；今天则只刷 Redis TTL 600s。DB 每账号每日最多 1 次 UPDATE，成本可忽略。连续不活跃 7 天后失效，需重登 |
| 子 Token 有效期 | 永不过期（`expires_at IS NULL`） | 子账号设备一次绑定即长期可用；父端点「下线子端」走主动吊销（写 revoked_at + 清 Redis） |
| 子账号绑定 | 方案 A：短 TTL `bind_token` → QR → 扫码换永久 child token | bind_token 5 分钟 TTL 存 Redis；兑换一次即删；不走密码 |
| Family 数据模型 | `family_members` 关联表预留 N 父；MVP 同时维护 `users.family_id` 冗余 | 冗余简化 MVP 查询；未来加入已有家庭（邀请码流程）只往 `family_members` 加行，不需迁移 |
| 设备绑定 | 强制：`auth_tokens.device_id NOT NULL`  • 中间件比对 `X-Device-Id` header | 登录 / 绑定 / 每次请求客户端必须传 `device_id`（Expo SecureStore 持久化的 UUID v4）。中间件比对 header 与 `auth_tokens.device_id`，不匹配立即吊销 token + 401 `device_changed`。`device_info JSONB` 独立字段存 `{ua, ip, platform}` 审计用，保持 nullable。此方案使「多设备挤下线」需求被天然满足（新设备登录即吊销旧设备 token），已入待办标记 obsolete |
| 鉴权中间件 | FastAPI `Depends`：`get_current_account` / `require_parent` / `require_child` | `get_current_account` 从 `Authorization: Bearer <token>` 解析 → Redis 查 → 未命中查 DB → 写回 Redis → 返 `CurrentAccount` Pydantic 模型。`require_parent` / `require_child` 在其上加 role 断言 |
| API 响应屏蔽 | `admin_note` / `password_hash` 从不进 Pydantic response schema | `app/schemas/accounts.py` 的 `AccountOut` 只含 `id / role / phone / family_id / is_active` |
| 运维 CLI | `python -m app.scripts.create_parent --note "..."` / `reset_parent_password --phone xxxx` | 自动随机 phone + 8 位密码；note 写入 `admin_note`；明文密码打印到控制台**仅此一次**。重置脚本同理 |
| 测试策略 | 业务逻辑（哈希 / token 签发 / 鉴权）走 TDD；Alembic / CLI 配手动验证 | `tests/` 下按模块分：`test_password.py` / `test_tokens.py` / `test_auth_middleware.py` / `test_login_api.py` / `test_child_bind.py` |
| 测试 DB | 真 PostgreSQL（`littlebox_test` 独立库）+ 外层 transaction + nested savepoint 回滚 | 复用现有 `postgres` 容器；session fixture `DROP/CREATE` 测试库 + `alembic upgrade head`；function fixture 开 savepoint，`session.commit()` 实际是 release savepoint。避开 SQLite：schema 重度依赖 JSONB / PG Enum / `gen_random_uuid()` / partial unique index |
| 测试 Redis | fakeredis（`fakeredis.aioredis.FakeRedis`）进程内模拟 | 不共享真 Redis；支持 `setex` / `pipeline(transaction=True)` / `expire` 等 M4 用到的全部 API（fakeredis ≥ 2.20） |

---

## 三方库文档核验

| 库 | 版本基线 | 关键 API / 推荐写法 | 避开的坑 |
| --- | --- | --- | --- |
| argon2-cffi | >= 23.1.0（最新 25.x） | `PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)`；`hash(password: str) -> str`；`verify(hash, password) -> bool`（失败抛 `VerifyMismatchError`）；`check_needs_rehash(hash) -> bool` 用于参数升级 | 不要手动拼 salt；`VerifyMismatchError` 必须单独 catch，其它异常（`VerifyError` / `InvalidHashError`）不能当成「密码错」返回（得 500 或记审计） |
| FastAPI Depends | 随 fastapi[standard] | `async def get_current_account(authorization: Annotated[str, Header()] = ...) -> CurrentAccount`；嵌套 `require_parent(acc: Annotated[CurrentAccount, Depends(get_current_account)])` | 不要把 `get_db()` 和 `get_current_account()` 合并 —— DB session 依赖链要纯净；OAuth2PasswordBearer 不适合自定义 header 传递 token，直接用 `Header()` 更透明 |
| SQLAlchemy 2.x async | 随 M1 基线 | `AsyncSession`  • `select()`  • `scalar_one_or_none()`；所有 session 走 `async_sessionmaker` 工厂 + FastAPI Depends 注入；`server_default=text("...")` 用于 DDL 默认值 | partial unique index 必须用 `Index(..., postgresql_where=text("..."))`，否则 Alembic autogenerate 会每次都想删掉重建 |
| Alembic | 随 M1 基线 | autogenerate 后**必须人工审阅**：partial index / Enum 增删 / 非空迁移默认值；`op.add_column` 对已有行给默认值用 `server_default=text(...)` 或两步迁移 | users 表已有数据（M1-M3 种子）的话，新增 NOT NULL 列必须先加 nullable + 回填 + alter not null；M4 阶段 DB 可能只有空表，但脚本要写得向前兼容 |
| redis-py 5.x (`redis[hiredis]`) | 随 M1 基线 | `redis.asyncio.Redis.from_url(...)`；`setex(key, seconds, value)` / `get(key)` / `delete(key)`；value 用 `json.dumps` 序列化 | `from_url` 返回的客户端连接池默认 `max_connections=50`，FastAPI 多 worker 场景够用；连接池在 `main.py` 的 lifespan 里创建 + 关闭，不要每次请求新建 |
| secrets（标准库） | — | `secrets.token_urlsafe(32)` 生成 token（43 字符 base64）；`secrets.choice(alphabet)` 生成随机 phone / password | 绝不用 `random` 模块（非加密安全） |
| hashlib（标准库） | — | `hashlib.sha256(token.encode()).hexdigest()` —— Token 入 DB 和 Redis 前都走 sha256，原 token 只在首次签发时返回客户端 | 不要对密码用 sha256（用 argon2）；token 哈希用 sha256 是因 token 本身是 256 位高熵随机串，无需再加 salt 防撞库 |
| httpx | >= 0.28（测试 fixture 用） | `httpx.AsyncClient(transport=ASGITransport(app=...), base_url="http://testserver")`；0.28 起 `AsyncClient(app=...)` 已 deprecated，必须走 `ASGITransport` 显式包装 | 不要沿用 `AsyncClient(app=app)` 旧写法；`async with` 管理生命周期避免连接池泄漏 |
| fakeredis | >= 2.20（测试 fixture 用） | `fakeredis.aioredis.FakeRedis(decode_responses=True)`；支持 `setex` / `expire(nx=True)` / `pipeline(transaction=False)` / `aclose`；进程内模拟 | 2.20 以下不支持 `pipeline(transaction=True)`；`decode_responses` 必须与生产 Redis 客户端保持一致，否则业务代码遇 bytes/str 混用 |

<aside>
⚠️

**Step 0 裸调核验**：Step 1 依赖加入后，先写 scratch 脚本跑一次 `PasswordHasher(...).hash("test")` + `verify(...)`，确认 argon2-cffi 在 Python 3.14 下 import / 调用正常，并记录 hash 字符串长度（`$argon2id$v=19$m=65536,t=3,p=4$...` 约 100 字符，`password_hash VARCHAR(255)` 留足余量）。

</aside>

---

## 关键架构决策

### Token 生命周期

```
登录成功
  → secrets.token_urlsafe(32)  生成明文 token
  → token_hash = sha256(token)
  → auth_tokens 插入 { user_id, token_hash, expires_at(7d 或 NULL), device_info }
  → Redis setex("auth:" + token_hash, 600, json({user_id, role, family_id, expires_at, last_rolled_date=today_cst}))
  → 返回明文 token 给客户端（仅此一次）

每次请求
  → Authorization: Bearer <token>
  → token_hash = sha256(token)
  → Redis get("auth:" + token_hash)
    命中：  JSON 解码 → 检查 expires_at 未过期
          → 若 expires_at 非 NULL 且 last_rolled_date != today(Asia/Shanghai)：
               DB UPDATE auth_tokens.expires_at = NOW() + 7d
               Redis 刷新 payload（last_rolled_date=today_cst）+ TTL 600s
          → 否则只刷 Redis TTL 600s（不写 DB）
          → 返 CurrentAccount
    未命中：DB 查 auth_tokens WHERE token_hash=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())
           未找到 → 401
           找到   → 构造 payload（last_rolled_date 初始化为 expires_at - 7d 的日期，触发首次续期判定）→ 写回 Redis 600s TTL → 返 CurrentAccount

主动吊销
  → auth_tokens.revoked_at = NOW()
  → Redis delete("auth:" + token_hash)
  → 下次请求 Redis miss → DB 查到 revoked_at 非空 → 401
```

### 子账号绑定（方案 A）

```
父端点「添加孩子」
  → POST /api/v1/children  body: { nickname, birth_date, gender }
  → 创建 users(role=child, is_active=true, family_id=parent.family_id) + child_profiles
  → 返回 child_user_id

父端点「生成绑定二维码」
  → POST /api/v1/children/{child_user_id}/bind-token
  → 断言 child 属于当前 parent 的 family
  → bind_token = secrets.token_urlsafe(16)
  → Redis setex("bind:" + bind_token, 300, json({parent_user_id, child_user_id}))
  → 返回 bind_token（前端渲染二维码）

子端扫码换 Token
  → POST /api/v1/auth/redeem-bind-token  body: { bind_token, device_info? }
  → Redis get → 兑换（deleteAfterGet）
  → 签永久 child token（expires_at=NULL）
  → 返回 { token, user: { id, role="child", ... } }
```

### Family N 父预留

- **MVP 写入**：`create_parent` CLI 创建 parent 时，同时在 `family_members` 写 `(family_id, parent_user_id, role=parent, joined_at=now())`。
- **MVP 读取**：所有查询仍走 `users.family_id`（单字段 FK，简单）。`family_members` 只写不读。

### 鉴权中间件设计

```
get_current_account (Depends)
  ↓
CurrentAccount { id, role, family_id, expires_at }
  ↓ ↓
require_parent     require_child
(断言 role==parent) (断言 role==child)
  ↓                  ↓
路由 handler         路由 handler
```

---

## 共享类型 / Schema 清单

### `app/schemas/accounts.py`（新增）

```python
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.models.enums import Gender, UserRole

class AccountOut(BaseModel):
    """对外返回的账号信息。**严禁**包含 password_hash / admin_note。"""
    id: uuid.UUID
    role: UserRole
    family_id: uuid.UUID
    phone: Optional[str] = Field(
        default=None,
        description="MVP 阶段为 4 位小写字母；PNVS 上线后为真手机号",
    )
    is_active: bool

class CurrentAccount(BaseModel):
    """鉴权中间件注入到 handler 的轻量账号上下文。"""
    id: uuid.UUID
    role: UserRole
    family_id: uuid.UUID
    expires_at: Optional[datetime] = Field(
        default=None, description="None 表示永不过期（子账号）"
    )

class LoginRequest(BaseModel):
    phone: str = Field(min_length=4, max_length=32)
    password: str = Field(
        min_length=8,
        max_length=128,
        description=(
            "MVP 特供；PNVS 上线后 LoginRequest 整体替换为短信验证流程（新增 "
            "/auth/login-sms 端点，body 改为 {phone, sms_code}），本字段随端点废弃"
        ),
    )
    device_id: str = Field(
        min_length=1,
        max_length=255,
        description="客户端 UUID v4；Expo SecureStore 持久化；设备变化即触发老 token 吊销",
    )

class LoginResponse(BaseModel):
    """登录成功响应。

    注意：/auth/login 内部会走 revoke_all_active_tokens，即便同一设备复登也会把
    「上一次的 token」一并吊销（语义干净优先；见决策背景页 §4.2）。前端复登后
    必须用本响应里的新 token 覆盖本地存储，不要保留旧 token。
    """
    token: str = Field(description="Opaque token，43 字符 base64")
    account: AccountOut

class CreateChildRequest(BaseModel):
    nickname: Optional[str] = Field(default=None, max_length=50)
    birth_date: Optional[date] = None
    gender: Optional[Gender] = None

class BindTokenResponse(BaseModel):
    bind_token: str = Field(description="5 分钟 TTL，一次性使用")
    expires_in_seconds: Literal[300] = 300

class RedeemBindTokenRequest(BaseModel):
    bind_token: str
    device_id: str = Field(
        min_length=1,
        max_length=255,
        description="子端设备 UUID；持久化到 auth_tokens.device_id（NOT NULL）",
    )
    device_info: Optional[dict] = Field(
        default=None,
        description="可选元数据（ua / platform / ip），存入 auth_tokens.device_info 审计用",
    )

class BindTokenStatusOut(BaseModel):
    """父端轮询子端扫码状态用。MVP 无推送基座（推送在 M10/M11），
    父端 QR 页面走 5 秒轮询 GET /api/v1/bind-tokens/{bind_token}/status：
    - status="pending" → 子端尚未扫码（bind_token 还活着）
    - status="bound"   → 子端已扫码兑换成功，父端可自动跳转 + 记录 child_user_id
    - 404 not_found    → bind_token 已过期且未兑换，父端应重新生成
    查询纯走 Redis（O(1) 内存 GET），不打 DB。详见决策背景 §1.2。"""
    status: Literal["pending", "bound"]
    child_user_id: Optional[uuid.UUID] = Field(
        default=None, description="status=bound 时返回；pending 时为 None",
    )
    bound_at: Optional[datetime] = Field(
        default=None, description="status=bound 时子端完成兑换的 UTC 时间",
    )
```

### `app/models/accounts.py`（增量修改）

只展示新增 / 修改部分，其余字段维持 M2 定义。

```python
# users 表：新增 password_hash / admin_note 字段
class User(BaseMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # Partial unique index：只对活跃的 parent 账号 phone 去重
        # child 没 phone（NULL），被禁用的 parent 不占号段
        Index(
            "idx_users_phone_parent_active",
            "phone",
            unique=True,
            postgresql_where=text("role = 'parent' AND is_active = true"),
        ),
    )
    # ... 其余字段同 M2 ...
    phone: Mapped[Optional[str]] = mapped_column(
        String(20),  # M2 基线不变；E.164 最多 16 字符，20 已留余量
        nullable=True,
        comment="MVP: 4 位小写字母; PNVS 后: 真手机号（E.164）。仅 parent 账号填",
    )
    password_hash: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="argon2id 哈希；MVP parent 必填；PNVS 后新账号 NULL；保留半年灰度期后 drop",
    )
    admin_note: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="运维内部备注，不暴露前端（API 响应屏蔽）",
    )

# auth_tokens 表：device_id 改 NOT NULL；新增 device_info 字段
class AuthToken(BaseMixin, Base):
    __tablename__ = "auth_tokens"
    # ... 其余字段同 M2 ...
    device_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="客户端持久化 UUID；登录 / 绑定时写入；中间件比对 X-Device-Id header",
    )
    device_info: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="设备元数据审计：{ua, ip, platform, ...}；可选",
    )

# 新增 family_members 关联表
class FamilyMember(BaseMixin, Base):
    """家庭成员关联。预留一家 N 父；MVP 每 family 1 行，CLI 创建父账号时自动写。

    读取路径 MVP 仍走 users.family_id，本表 MVP 只写不读。
    未来加入已有家庭（邀请码）流程落地时切到 JOIN 本表的读取路径。
    """
    __tablename__ = "family_members"
    __table_args__ = (
        Index("idx_family_members_family", "family_id"),
    )

    family_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("families.id"), nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"),
        unique=True, nullable=False,
        comment="MVP 一个 user 只属一个 family；N 父也是不同 user_id",
    )
    role: Mapped[UserRole] = mapped_column(nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
```

---

## 文件结构

本里程碑新增 / 改动：

```
backend/
├── app/
│   ├── auth/                         # M4 新增模块
│   │   ├── __init__.py
│   │   ├── password.py               # argon2 哈希封装 + 随机生成器
│   │   ├── tokens.py                 # Token 签发 / 校验 / 吊销（含 Redis 缓存）
│   │   ├── bind.py                   # bind_token 生成 + 兑换
│   │   ├── deps.py                   # FastAPI Depends：get_current_account / require_*
│   │   └── redis_client.py           # Redis 连接池 lifespan
│   ├── api/
│   │   ├── auth.py                   # POST /api/v1/auth/login / logout / redeem-bind-token
│   │   └── children.py               # POST /api/v1/children + /{id}/bind-token（需 require_parent）
│   ├── schemas/
│   │   └── accounts.py               # AccountOut / CurrentAccount / LoginRequest 等
│   ├── models/
│   │   └── accounts.py               # 修改：users 加字段 / 部分索引；auth_tokens 加 device_info；新增 FamilyMember
│   ├── scripts/                      # M4 新增运维脚本
│   │   ├── __init__.py
│   │   ├── _common.py                # 共享字母表 / 随机生成器
│   │   ├── create_parent.py          # python -m app.scripts.create_parent --note "..."
│   │   └── reset_parent_password.py  # python -m app.scripts.reset_parent_password --phone xxxx
│   ├── config.py                     # 新增 redis_url / token_ttl_parent_days 字段
│   └── main.py                       # 挂新路由 + Redis lifespan
├── alembic/versions/
│   └── <hash>_m4_accounts_auth.py    # users 加字段 + 部分索引；auth_tokens 加 device_info；新增 family_members
├── tests/
│   ├── conftest.py                    # M4 新增：DB fixtures (savepoint) + fakeredis + FastAPI deps override
│   ├── test_password.py              # argon2 封装单测
│   ├── test_tokens.py                # Token 签发 / 校验 / 吊销（含 Redis mock）
│   ├── test_auth_deps.py             # get_current_account / require_* 单测
│   ├── test_login_api.py             # POST /auth/login 集成测试
│   ├── test_child_bind.py            # 子账号创建 + 绑定兑换
│   └── test_scripts.py               # create_parent / reset_parent_password CLI 冒烟
└── pyproject.toml                    # 新增 argon2-cffi；dev 新增 fakeredis>=2.20
```

---

## 测试基础设施（conftest）

在任何 TDD Step 开始前，先把 `tests/conftest.py` 和配置项落地，避免后续 Step 重复贴同样的 fixture 样板。本章节目标：

- 测试与开发用同一个 `postgres` 容器，但数据库名独立（`littlebox_test`），互不污染
- 业务代码里的 `await db.commit()` 在测试中不会真污染数据库 → 借 SQLAlchemy 2.0 的 `join_transaction_mode="create_savepoint"`，外层 transaction 包裹 + function teardown rollback
- Redis 完全走内存 fakeredis；不引入对真 Redis 容器的测试依赖
- FastAPI 路由测试通过 `app.dependency_overrides` 注入测试 session / fakeredis，不改其它商业代码

### Step T0：依赖与配置

- [x]  `pyproject.toml` 的 `[project.optional-dependencies].dev` 新增：
    - `fakeredis>=2.20.0`（已包含 `aioredis` 兼容层；必须 2.20+ 才支持 `pipeline(transaction=True)` + `aclose`）
    - `pytest-asyncio` 已在 M3 引入，无需新增
- [x]  `pyproject.toml` 新增 / 校验 pytest 配置块：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
markers = [
    "live: needs real LLM API",
]
```

> `asyncio_default_fixture_loop_scope="session"` 必须设：SQLAlchemy async engine 不能跨 event loop 共享；没此项时 pytest-asyncio 0.23+ 默认给每个测试新建 loop → session-scope engine fixture 站着旧 loop 报 `RuntimeError: attached to a different loop`。
> 

### Step T1：Alembic `env.py` 必须改（M1 实际为硬编码覆盖）

`depends_on: [T0]`

> 背景：M1 落地版硬编码覆盖 `sqlalchemy.url`，会让测试在开发库上跑 migration（灾难级污染）。详见 [M4 · 决策背景与避坑记录](https://www.notion.so/M4-7d37f77eaeeb49b388c96dc72140beac?pvs=21) §2。
> 

**目标**：cfg 优先 else settings 回落；生产 / 开发仍走 settings，只给测试开显式通道。offline / online 两条分支都要在动用 `async_engine_from_config` / `engine_from_config` 前命中。

- [x]  打开 `backend/alembic/env.py`，把上述硬编码行改为：

```python
# 配置源优先级：cfg 参数（测试场景） > settings.database_url（生产 / 开发）
# 注：本文件被同步 API（command.upgrade/revision）与 async API 两路复用
#     run_migrations_offline / run_async_migrations 都要在自己的入口处调这一行
url = config.get_main_option("sqlalchemy.url") or settings.database_url
config.set_main_option("sqlalchemy.url", url)
```

- [x]  确认 offline / online 两条分支都在动用 `async_engine_from_config` / `engine_from_config` 前命中这段逻辑（M1 只在 `run_async_migrations` 里设，补全时二者都要）
- [x]  验证：
    - `docker compose exec api alembic current` 正常输出（回归原有开发流）
    - 写个临时脚本：`Config("alembic.ini")` → `cfg.set_main_option("sqlalchemy.url", "postgresql+asyncpg://lb:lb@postgres:5432/littlebox_test")` → `command.upgrade(cfg, "head")` → 连 `littlebox_test` 不是 `littlebox`，确认测试通道生效

**提交**：`fix(alembic): let cfg url override settings for test fixtures`

### Step T1.5：app stub 骨架(为 T2 conftest 铺路)

`depends_on: [T1]`

> T2 conftest 会 `from app.auth.redis_client import get_redis` 和 `from app.main import create_app`,若两个 import 失败整个 pytest 无法加载。Step T1.5 只铺空壳:真实 lifespan 由 Step 5 补齐,路由由 Step 6 挂载。选择「先 stub 后补」而非把 redis_client / create_app 提前到 T2 之前完整实现,是为了保持「基础设施 Step T*」与「业务 Step N」的职责清晰。见 [M4 · 决策背景与避坑记录](https://www.notion.so/M4-7d37f77eaeeb49b388c96dc72140beac?pvs=21) §9。
> 
- [x]  新建 `app/auth/__init__.py`(空文件)
- [x]  新建 `app/auth/redis_client.py` 骨架(Step 5 补 lifespan):

```python
"""Redis 连接工厂 + FastAPI Depends;Step 5 补充 lifespan。"""
from redis.asyncio import Redis

_redis: Redis | None = None

async def get_redis() -> Redis:
    assert _redis is not None, "redis pool not initialized (check lifespan)"
    return _redis
```

- [x]  改 `app/main.py`:抽出 `create_app()` factory,内部暂只 `return app`;路由 Step 6 挂,lifespan Step 5 挂:

```python
from fastapi import FastAPI

def create_app() -> FastAPI:
    app = FastAPI()
    # routers / lifespan 在后续 Step 注入
    return app

app = create_app()
```

**验证**:`docker compose exec api python -c "from app.auth.redis_client import get_redis; from app.main import create_app; print(create_app())"` 不报 ImportError。

**提交**:`chore(app): scaffold auth.redis_client and main.create_app for test bootstrap`

### Step T1.6:`app/db.py` session 工厂骨架(计划与基线差异补救)

`depends_on: [T1.5]`

> 背景:M4 计划在 Step 4/6/7 隐式引用 `from app.db import get_db`,但 M1 Step 4 只建了 `alembic/env.py`(迁移专用自建 engine),M2 Step 1「ORM 基础设施」只补 `BaseMixin`,M3 streaming 不打 DB —— `app.db` 模块与 `get_db` dependency 从未在基线中创建。T2 conftest 的 `from app.db import get_db` / `dependency_overrides[get_db]` 直接撞 ImportError。详见 [M4 执行偏差记录](https://www.notion.so/M4-9341dc6b128f448ea1aeaa733de9ae81?pvs=21) T1.5.1。
> 
- [ ]  新建 `app/db.py`:

```python
"""FastAPI async SQLAlchemy session 工厂。业务 handler 走 commit_with_redis,
不要在 yield 后显式 commit/close。CLI 不复用本模块的 _engine,走 _common.cli_runtime()。"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
_session_maker = async_sessionmaker(_engine, expire_on_commit=False)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _session_maker() as session:
        yield session

async def dispose_engine() -> None:
    """挂到 main.py lifespan shutdown;Step 5 Redis lifespan 落地时一并挂。"""
    await _engine.dispose()
```

- [ ]  `app/main.py` 的 `create_app()` **暂不挂 lifespan**(Step 5 与 `redis_lifespan` 合并一起挂);本 Step 只确保 module import 链通畅
- [ ]  验证:
    - `docker compose exec api python -c "from app.db import get_db; print(get_db)"` → `<function get_db at 0x...>`
    - `docker compose exec api python -c "from app.main import create_app; app=create_app(); print([r.path for r in app.routes])"` → 路由表与 T1.5 完全一致(6 条)
    - `docker compose exec api python -c "import asyncio; from app.db import _session_maker; asyncio.run((lambda: (lambda s: print(type(s).__name__))(_session_maker()))())"` → `AsyncSession`(确认 engine 能真的起 session,不实际查表)

**提交**:`chore(app): scaffold app.db engine and get_db dependency for test bootstrap`

### Step T2:`tests/conftest.py`

`depends_on: [T1]`

创建 `backend/tests/conftest.py`，内容如下（**照抄即可**，已针对已知坍优化）：

```python
"""M4 新增：Auth + DB 测试基础设施。

设计要点：
- DB：真 PostgreSQL，独立 `littlebox_test` 库；session 开始 DROP/CREATE + alembic upgrade head；
  function 每测试外层 transaction + nested savepoint，业务 `session.commit()` 实际 release savepoint，
  teardown rollback 外层 → 零持久化 → 测试完全隔离
- Redis：fakeredis 进程内模拟，每测试独立实例
- FastAPI：`dependency_overrides` 注入 `get_db` / `get_redis` 指向测试 fixture
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest_asyncio
from alembic import command
from alembic.config import Config
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.redis_client import get_redis
from app.config import settings
from app.db import get_db
from app.main import create_app

TEST_DB_NAME = "littlebox_test"

def _base_url() -> str:
    """从 settings.database_url 派生，保证 host / port / 凭证 与开发一致。
    用 render_as_string(hide_password=False)：SQLAlchemy URL 的 __str__ 默认会把
    密码遮蔽成 ***，create_async_engine 拿到 *** 直接触发 asyncpg 密码认证失败。
    详见决策背景 §11.1。"""
    return make_url(settings.database_url).render_as_string(hide_password=False)

def _admin_url() -> str:
    # postgres 镜像默认存在的维护库；用于 DROP/CREATE 测试库
    return (
        make_url(settings.database_url)
        .set(database="postgres")
        .render_as_string(hide_password=False)
    )

def _test_url() -> str:
    return (
        make_url(settings.database_url)
        .set(database=TEST_DB_NAME)
        .render_as_string(hide_password=False)
    )

# ---------- session scope：建库 + migration ----------

@pytest_asyncio.fixture(scope="session")
async def _bootstrap_test_db() -> AsyncGenerator[None, None]:
    """每跑一轮测试：断开测试库残留连接 → DROP → CREATE → alembic upgrade head。"""
    admin_engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{TEST_DB_NAME}' AND pid <> pg_backend_pid()"
            ))
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    finally:
        await admin_engine.dispose()

    # alembic command.upgrade 是同步 API，内部会起自己的 loop 跑 async env.py。
    # pytest 已在 session loop 中，用 executor 隔离避免 loop 冲突。
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _test_url())
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, command.upgrade, cfg, "head")

    yield
    # 不主动 drop：保留库以便 CI 失败后下载骨现场；下次 session 头会重建

@pytest_asyncio.fixture(scope="session")
async def engine(_bootstrap_test_db: None) -> AsyncGenerator[AsyncEngine, None]:
    # NullPool 必选：pytest-asyncio 为每个测试函数创建独立 event loop，engine 若
    # 缓存连接会跨 loop 复用 asyncpg 协议对象 → Future attached to a different
    # loop。NullPool 每次 connect 建新连接、close 即销毁，彻底规避跨 loop。
    # savepoint 模式完整保留。详见决策背景 §11.3。
    eng = create_async_engine(_test_url(), poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()

# ---------- function scope：每测试 savepoint 隔离 ----------

@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """外层 connection + begin；注入 session 时 `join_transaction_mode="create_savepoint"`。
    业务代码 `await session.commit()` 实际只 release 内层 savepoint，不会落盘。
    teardown 外层 rollback → 本测试所有写入全部丢弃。
    """
    async with engine.connect() as connection:
        trans = await connection.begin()
        session = AsyncSession(
            bind=connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,  # savepoint release 后仍可访问 ORM 属性
        )
        try:
            yield session
        finally:
            # 护栏：漏调 commit_with_redis 的测试会在此抛 AssertionError（§8 封装）
            pending = session.info.get("pending_redis_ops") or []
            await session.close()
            if trans.is_active:
                await trans.rollback()
            assert not pending, (
                "pending redis ops not flushed — use commit_with_redis() "
                "instead of bare db.commit()"
            )

# ---------- function scope：fakeredis ----------

@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[FakeRedis, None]:
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()

# ---------- function scope：FastAPI ASGI client ----------

@pytest_asyncio.fixture
async def app(db_session: AsyncSession, redis_client: FakeRedis) -> AsyncGenerator[FastAPI, None]:
    application = create_app()

    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _get_redis() -> FakeRedis:
        return redis_client

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_redis] = _get_redis
    try:
        yield application
    finally:
        application.dependency_overrides.clear()

@pytest_asyncio.fixture
async def api_client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

# ---------- 业务高层便捷 fixtures（后续 Step 复用） ----------

@pytest_asyncio.fixture
async def seeded_parent(db_session: AsyncSession):
    """种一个 active parent + family + family_members。返回 (user, plaintext_password)。"""
    from app.auth.password import generate_password, generate_phone, hash_password
    from app.models.accounts import Family, FamilyMember, User
    from app.models.enums import UserRole

    pw = generate_password()
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.parent,
        phone=generate_phone(),
        password_hash=hash_password(pw),
        is_active=True,
        admin_note="test parent",
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.parent))
    await db_session.commit()  # 实际 release savepoint
    return user, pw
```

> 避坑细节（savepoint 模式 / expire_on_commit / loop scope / xdist 补丁 等 7 条）详见 [M4 · 决策背景与避坑记录](https://www.notion.so/M4-7d37f77eaeeb49b388c96dc72140beac?pvs=21) §3。当前 conftest 在 `TEST_DB_NAME` 定义旁留 `# TODO(xdist): 开 pytest-xdist 时按 worker 隔离库名` 注释占位。
> 

**验证步骤**（conftest 落地后立即跑一遍，确认基础设施无故障再进 Step 2）：

- [ ]  写一个 “smoke test” 在 `tests/test_conftest_smoke.py`：

```python
"""conftest 基础设施自检；确认后可删或保留。"""
import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

async def test_db_session_round_trip(db_session):
    """能建表入数，commit 足够企业用但不落盘。"""
    from app.models.accounts import Family
    fam = Family()
    db_session.add(fam)
    await db_session.commit()  # 实际 release savepoint
    got = await db_session.scalar(text("SELECT count(*) FROM families"))
    assert got == 1

async def test_db_isolation_across_tests_1(db_session):
    from app.models.accounts import Family
    db_session.add(Family())
    await db_session.commit()

async def test_db_isolation_across_tests_2(db_session):
    """上个测试写入的 Family 应该已被 rollback，这里 count 必须为 0。"""
    count = await db_session.scalar(text("SELECT count(*) FROM families"))
    assert count == 0

async def test_redis_round_trip(redis_client):
    await redis_client.setex("foo", 60, "bar")
    assert await redis_client.get("foo") == "bar"

async def test_api_client_health(api_client):
    r = await api_client.get("/health")
    assert r.status_code == 200
```

- [ ]  `docker compose exec api pytest tests/test_conftest_smoke.py -v` 全绿
- [ ]  清理：smoke 测试确认无问题后可保留（作为基础设施回归监控）

**提交**：`test(infra): add conftest with pg savepoint + fakeredis + fastapi deps override`

---

## 执行步骤

### Step 0：依赖引入 + argon2 裸调核验

`depends_on: none`

- [ ]  `backend/pyproject.toml` 新增 `argon2-cffi>=23.1.0`
- [ ]  `docker compose build api`
- [ ]  临时 scratch 脚本裸调 `argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)` 的 `hash` / `verify` / `check_needs_rehash`，记录：
    - 哈希字符串实际长度（确认 VARCHAR(255) 足够）
    - Python 3.14 下 `cffi` 能正常加载
    - `verify` 失败抛的具体异常类（`VerifyMismatchError` vs `VerifyError`）
- [ ]  scratch 脚本不入库，实测数据记入 `M4 执行偏差记录` 附录

**验证**：`docker compose exec api python -c "from argon2 import PasswordHasher; ph=PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4); h=ph.hash('abcdefgh'); print(len(h), ph.verify(h, 'abcdefgh'))"` 输出长度 < 255 + `True`

**提交**：`chore(backend): add argon2-cffi dependency`

---

### Step 1：Alembic migration — users 加字段 + 部分索引 + device_info + family_members

`depends_on: [0]`

- [ ]  修改 `app/models/accounts.py`：
    - 新增 `User.password_hash`、`User.admin_note`
    - 新增 `User.__table_args__` partial unique index `idx_users_phone_parent_active`
    - 新增 `AuthToken.device_info: Mapped[Optional[dict]]`（JSONB nullable，审计用）
    - `AuthToken.device_id`：`nullable=True` → `nullable=False`（设备绑定）
    - 新增 `FamilyMember` 类
- [ ]  更新 `app/models/__init__.py` 导出 `FamilyMember`
- [ ]  `docker compose exec api alembic revision --autogenerate -m "m4: accounts auth fields and family_members"`
- [ ]  **审阅 migration 文件**：
    - partial index 生成为 `op.create_index(..., postgresql_where=sa.text("role = 'parent' AND is_active = true"))`，不是普通 unique
    - 新增两列 `password_hash` / `admin_note` 都是 `nullable=True`（M4 阶段不回填历史数据；CLI 创建新账号时写入）
    - `auth_tokens.device_info` 新增 nullable JSONB
    - `auth_tokens.device_id` 从 nullable 改 NOT NULL：M4 阶段表内无历史行，直接 `alter_column(..., nullable=False)` 即可；**若有历史行**，先填默认值 `'legacy-null'` 再 alter
    - `family_members` 表按依赖顺序创建（families 已存在，users 已存在 → FM 放在最后）
- [ ]  `alembic upgrade head`
- [ ]  `alembic downgrade -1` + `upgrade head` 往返一次，确认可逆

**验证**：

- **前置断言**：`docker compose exec postgres psql -U lb -d littlebox -c "SELECT count(*) FROM auth_tokens;"` 返回 0 → 确认无历史行需回填，可直接 `alter_column(..., nullable=False)`（决策背景 §10.2）
- `\d+ users` 看到新字段 + partial index 存在
- `\d+ auth_tokens` 看到 `device_info jsonb`
- `\d family_members` 表存在，FK 指向 `families` / `users`
- `SELECT role FROM users WHERE phone IS NULL;` child 账号仍然 NULL 正常

**提交**：`feat(db): m4 accounts auth schema (password_hash, admin_note, device_info, family_members)`

---

### Step 2：password 模块（TDD）

`depends_on: [0]`

> Step 2 是纯函数单测，不依赖 Step 1 的 DB schema；可与 Step 1 并行开发。
> 

**阶段 A（Red）** — 写失败测试 + 声明接口：

- [ ]  `app/auth/password.py` 骨架：

```python
"""密码哈希与随机生成。argon2id + OWASP 2024 参数。"""
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ALPHABET = "abcdefghjkmnpqrstuvwxyz"  # 去 i/l/o

_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

def hash_password(password: str) -> str:
    """argon2id 哈希。返回 `$argon2id$...` 字符串，入 DB 的 password_hash 列。"""
    raise NotImplementedError

def verify_password(hashed: str, password: str) -> bool:
    """验证密码。输入 mismatch 返回 False；其它异常向上抛（记审计 / 500）。"""
    raise NotImplementedError

def generate_phone() -> str:
    """MVP 特供：4 位小写字母（字符集去 i/l/o）。"""
    raise NotImplementedError

def generate_password() -> str:
    """MVP 特供：8 位小写字母（字符集去 i/l/o）。"""
    raise NotImplementedError
```

- [ ]  `tests/test_password.py`：

```python
"""Given 密码明文 / When 走 hash+verify / Then 返回正确布尔值。
覆盖范围：hash 格式、正/反向验证、随机生成器的字符集与长度。
"""
import pytest

from app.auth.password import (
    generate_password,
    generate_phone,
    hash_password,
    verify_password,
)

_ALPHABET = set("abcdefghjkmnpqrstuvwxyz")

class TestHashAndVerify:
    def test_hash_returns_argon2id_string(self) -> None:
        """Given 任意明文 When hash_password Then 返回以 $argon2id$ 起头的字符串"""
        h = hash_password("abcdefgh")
        assert h.startswith("$argon2id$")
        assert len(h) < 255

    def test_verify_correct_password_returns_true(self) -> None:
        h = hash_password("correctpw")
        assert verify_password(h, "correctpw") is True

    def test_verify_wrong_password_returns_false(self) -> None:
        h = hash_password("correctpw")
        assert verify_password(h, "wrongpw") is False

class TestGenerators:
    @pytest.mark.parametrize("_", range(20))
    def test_phone_is_4_lowercase_letters(self, _: int) -> None:
        p = generate_phone()
        assert len(p) == 4
        assert set(p) <= _ALPHABET

    @pytest.mark.parametrize("_", range(20))
    def test_password_is_8_lowercase_letters(self, _: int) -> None:
        p = generate_password()
        assert len(p) == 8
        assert set(p) <= _ALPHABET
```

- [ ]  提交：`test(auth): add failing tests for password module`
- [ ]  运行 `pytest tests/test_password.py -v` 确认全红

**阶段 B（Green）** — 最小实现：

```python
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ALPHABET = "abcdefghjkmnpqrstuvwxyz"
_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

def hash_password(password: str) -> str:
    return _HASHER.hash(password)

def verify_password(hashed: str, password: str) -> bool:
    try:
        return _HASHER.verify(hashed, password)
    except VerifyMismatchError:
        return False
    # 其它 argon2 异常（InvalidHashError 等）属于数据损坏，向上抛

def generate_phone() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(4))

def generate_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))
```

**验证**：`pytest tests/test_password.py -v` 全绿

**提交**：`feat(auth): implement argon2 password hashing and random generators`

---

### Step 3：Token 模块（TDD）

`depends_on: [1]`

> 本 Step 业务写函数（issue / revoke / revoke_all / roll）直接以最终封装形态落地：DB 变更 + 对 Redis 的副作用 stage 到 [session.info](http://session.info)，延后由 `commit_with_redis` 统一 flush；读路径 cache 维护不经 staging。背景 / 替代方案评估见 [M4 · 决策背景与避坑记录](https://www.notion.so/M4-7d37f77eaeeb49b388c96dc72140beac?pvs=21) §7 / §8。
> 

**阶段 Pre — 封装入口** 新建 `app/auth/redis_ops.py`（业务代码对 Redis 的唯一写入通道）:

```python
"""DB↔Redis 同步的统一封装。业务代码不直接写 Redis，改为 stage 到 session.info，
由 commit_with_redis 先 DB commit 再 flush Redis；策略在此一点统一。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
_PENDING_KEY = "pending_redis_ops"

@dataclass
class RedisOp:
    kind: Literal["setex", "delete"]
    key: str
    ttl_seconds: int = 0
    value: str | None = None

def stage_redis_op(db: AsyncSession, op: RedisOp) -> None:
    """挂一条 Redis 操作到 session.info，由 commit_with_redis 统一 flush。
    session close / rollback 时自然丢弃。"""
    db.info.setdefault(_PENDING_KEY, []).append(op)

def discard_pending_redis_ops(db: AsyncSession) -> None:
    """显式丢弃（决定不 commit 但也不想触发 teardown 护栏时用）。"""
    db.info.pop(_PENDING_KEY, None)

async def commit_with_redis(db: AsyncSession, redis: Redis) -> None:
    """业务唯一推荐的 commit 入口：先 DB commit，再 flush 挂载的 Redis ops。

    - DB commit 报错 → ops 已 pop，直接丢弃；异常上抛；
    - DB commit 成功但 Redis flush 报错 → log error 不抛；业务语义由 DB 决定，
      Redis 是缓存允许临时不一致，下次 miss 回填或 TTL 到期自愈。
    """
    ops: list[RedisOp] = db.info.pop(_PENDING_KEY, [])
    await db.commit()
    if not ops:
        return
    try:
        async with redis.pipeline(transaction=False) as pipe:
            for op in ops:
                if op.kind == "setex":
                    assert op.value is not None
                    pipe.setex(op.key, op.ttl_seconds, op.value)
                elif op.kind == "delete":
                    pipe.delete(op.key)
            await pipe.execute()
    except Exception:
        logger.exception("redis flush failed after db commit; cache self-heals via TTL")
```

**提交（阶段 Pre）**：`feat(auth): add redis_ops staging helpers for db+redis sync`

**阶段 A（Red）** — `app/auth/tokens.py` 声明接口：

```python
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UserRole

class TokenPayload(BaseModel):
    """Redis 缓存与 DB 查询的统一返回形状。"""
    user_id: uuid.UUID
    role: UserRole
    family_id: uuid.UUID
    device_id: str  # 随 token 绑定；中间件比对 X-Device-Id header 免查 DB
    expires_at: Optional[datetime]
    # ISO date（北京时间）上一次滚动续期的日期；每日首次命中时触发 DB UPDATE
    last_rolled_date: Optional[str] = None

REDIS_KEY_PREFIX = "auth:"
REDIS_TTL_SECONDS = 600

def token_hash(token: str) -> str:
    """sha256 hex digest。入 DB 和 Redis key 的前缀哈希。"""
    raise NotImplementedError

async def issue_token(
    db: AsyncSession,
    user_id: uuid.UUID,
    role: UserRole,
    family_id: uuid.UUID,
    device_id: str,  # 必填：写入 auth_tokens.device_id (NOT NULL) + Redis payload
    *,
    ttl_days: Optional[int] = 7,  # None = 永不过期（child）
    device_info: Optional[dict] = None,
) -> str:
    """签新 token：DB 写 auth_tokens + stage Redis setex + 返回明文 token（仅此一次）。
    调用方在 issue_token 之前调 revoke_all_active_tokens 以保证「一次一设备」语义；
    调用链末尾必须 `await commit_with_redis(db, redis)` 才真正落盘并刷 Redis。"""
    raise NotImplementedError

async def resolve_token(
    db: AsyncSession,
    redis: Redis,
    token: str,
) -> Optional[TokenPayload]:
    """纯读：Redis 命中刷 TTL 返回；miss 查 DB 回填 Redis。不做 DB UPDATE；
    续期由 get_current_account 在判断 needs_roll 后显式调 roll_token_expiry。
    已吊销 / 已过期返回 None。"""
    raise NotImplementedError

def needs_roll(payload: TokenPayload) -> bool:
    """父 token 是否需要今天首次续期。子 token（expires_at=None）永远返 False。"""
    raise NotImplementedError

async def roll_token_expiry(
    db: AsyncSession, *, token_hash_hex: str, payload: TokenPayload,
) -> TokenPayload:
    """DB UPDATE expires_at +7d + stage Redis setex 新 payload；返回新 payload。
    调用方必须紧跟 `await commit_with_redis(db, redis)`。"""
    raise NotImplementedError

async def revoke_token(
    db: AsyncSession,
    token: str,
) -> None:
    """主动吊销单个 token：DB auth_tokens.revoked_at = NOW() + stage Redis delete。幂等。
    调用方必须紧跟 `await commit_with_redis(db, redis)`。"""
    raise NotImplementedError

async def revoke_all_active_tokens(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """批量吊销指定用户的全部活跃 token（DB + Redis 同步清）。

    用途：
      - parent 新设备登录前（/auth/login issue_token 之前）
      - child 新设备扫码前（/auth/redeem-bind-token issue_token 之前）
      - 父端「下线所有设备」按钮（POST /children/{id}/revoke-tokens）
      - 运维 reset_parent_password 脚本

    返回被吊销的 token 数量。对无活跃 token 的 user 幂等返回 0。
    调用方必须紧跟 `await commit_with_redis(db, redis)`。
    """
    raise NotImplementedError
```

- [ ]  `tests/test_tokens.py` 覆盖：
    - `token_hash` 同输入同输出、长度 64 hex
    - `issue_token` → DB 存 sha256 + Redis 命中 + 返回明文；父 token `expires_at = now + 7d`；子 token（ttl_days=None）`expires_at IS NULL`
    - `resolve_token` Redis 命中路径（不查 DB）
    - `resolve_token` Redis miss → DB 命中 → 回填 Redis
    - `resolve_token` DB 中 `revoked_at` 非空 → 返回 None
    - `resolve_token` DB 中 `expires_at` 过期 → 返回 None
    - `revoke_token` → DB revoked_at 写入 + Redis 被清
    - `revoke_token` 对不存在的 token 不报错（幂等）
    - **resolve_token 不做续期**：mock 明日 → resolve_token 命中 → DB `expires_at` 保持原值（只刷 Redis TTL）；续期动作已下放到 get_current_account（见 Step 4 测试）
    - **needs_roll 判断**：明日 mock 下对 parent payload 返 True；同日再判 False；子 token payload（expires_at=None）任何时间永远 False
    - **roll_token_expiry 语义**：调 `roll_token_expiry(db, token_hash_hex=th, payload=p)` → DB `expires_at` 已 UPDATE（尚未 commit）+ `session.info` 新增一条 setex RedisOp；返回新 payload 的 `last_rolled_date=今日`
    - **roll_token_expiry 未 commit_with_redis 则不可见**：调后直接 `db_session.rollback()` → **用独立 AsyncSession 绕开外层 savepoint** 查 `auth_tokens` → expires_at 未变；Redis 也无写入
    - **roll_token_expiry + commit_with_redis 落盘**：调 roll_token_expiry 后 `await commit_with_redis(db, redis)` → 独立 AsyncSession 查 DB → expires_at 已续；Redis payload `last_rolled_date=今日`
    - **device_id 贯穿到 DB**：`issue_token(..., device_id="devA")` → `SELECT device_id FROM auth_tokens WHERE token_hash=?` 返回 `"devA"`
    - **device_id 贯穿到 Redis**：同上 → Redis 反序列化 payload → `payload.device_id == "devA"`
    - **issue_token 不传 device_id**：TypeError（必填参数；防止忘传导致 DB NOT NULL 违反）
    - **revoke_all_active_tokens 批量**：同 user 连续 3 次 issue（不同 device_id）→ 调 revoke_all → 返回 3；`auth_tokens` 该 user 所有行 `revoked_at IS NOT NULL`；3 个 Redis key 全被删
    - **revoke_all_active_tokens 幂等**：对无活跃 token 的 user 调 → 返回 0，无异常
    - **revoke_all_active_tokens 用户隔离**：user A 的 revoke_all 不影响 user B 的 token
- [ ]  Redis 和 DB fixtures 复用上方《测试基础设施》章节的 `db_session` / `redis_client`；本 Step 直接 inject，不在此处重新搭 fixture
- [ ]  提交：`test(auth): add failing tests for token module`

**阶段 B（Green）** — 最终实现（直接落地 stage + commit_with_redis 形态）：

```python
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_ops import RedisOp, stage_redis_op
from app.models.accounts import AuthToken, User
from app.models.enums import UserRole

# ... TokenPayload / REDIS_KEY_PREFIX / REDIS_TTL_SECONDS 同阶段 A ...

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def _redis_key(th: str) -> str:
    return f"{REDIS_KEY_PREFIX}{th}"

_CST = ZoneInfo("Asia/Shanghai")

def _today_cst() -> str:
    return datetime.now(_CST).date().isoformat()

async def issue_token(
    db: AsyncSession,
    user_id: uuid.UUID,
    role: UserRole,
    family_id: uuid.UUID,
    device_id: str,
    *,
    ttl_days: Optional[int] = 7,
    device_info: Optional[dict] = None,
) -> str:
    token = secrets.token_urlsafe(32)
    th = token_hash(token)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=ttl_days)
        if ttl_days is not None else None
    )
    db.add(AuthToken(
        user_id=user_id, token_hash=th, expires_at=expires_at,
        device_id=device_id, device_info=device_info,
    ))
    await db.flush()
    payload = TokenPayload(
        user_id=user_id, role=role, family_id=family_id,
        device_id=device_id, expires_at=expires_at,
        last_rolled_date=_today_cst() if expires_at is not None else None,
    )
    stage_redis_op(db, RedisOp(
        kind="setex", key=_redis_key(th),
        ttl_seconds=REDIS_TTL_SECONDS,
        value=payload.model_dump_json(),
    ))
    return token

async def resolve_token(
    db: AsyncSession, redis: Redis, token: str,
) -> Optional[TokenPayload]:
    """纯读路径：不做 DB UPDATE；续期由 get_current_account 调 roll_token_expiry。"""
    th = token_hash(token)
    cached = await redis.get(_redis_key(th))
    if cached is not None:
        payload = TokenPayload.model_validate_json(cached)
        if payload.expires_at is not None and payload.expires_at < datetime.now(timezone.utc):
            return None
        # 读路径 cache 维护：刷 TTL；失败下次 miss 自愈，不属业务状态
        await redis.expire(_redis_key(th), REDIS_TTL_SECONDS)
        return payload

    # Redis miss → 查 DB → 回填 Redis
    stmt = (
        select(AuthToken, User)
        .join(User, User.id == AuthToken.user_id)
        .where(AuthToken.token_hash == th, AuthToken.revoked_at.is_(None))
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    tok, user = row
    if tok.expires_at is not None and tok.expires_at < datetime.now(timezone.utc):
        return None

    # last_rolled_date 初始化为 (expires_at - 7d).date()，让外层 needs_roll
    # 能触发今天的首次续期（若今天尚未续）
    seed_date = (
        (tok.expires_at - timedelta(days=7)).astimezone(_CST).date().isoformat()
        if tok.expires_at is not None else None
    )
    payload = TokenPayload(
        user_id=user.id, role=user.role, family_id=user.family_id,
        device_id=tok.device_id, expires_at=tok.expires_at,
        last_rolled_date=seed_date,
    )
    # 读路径回填：不经 staging；失败下次 miss 重试
    await redis.setex(_redis_key(th), REDIS_TTL_SECONDS, payload.model_dump_json())
    return payload

def needs_roll(payload: TokenPayload) -> bool:
    return payload.expires_at is not None and payload.last_rolled_date != _today_cst()

async def roll_token_expiry(
    db: AsyncSession, *, token_hash_hex: str, payload: TokenPayload,
) -> TokenPayload:
    new_expires = datetime.now(timezone.utc) + timedelta(days=7)
    await db.execute(update(AuthToken).where(
        AuthToken.token_hash == token_hash_hex,
        AuthToken.revoked_at.is_(None),
    ).values(expires_at=new_expires))
    new_payload = payload.model_copy(update={
        "expires_at": new_expires,
        "last_rolled_date": _today_cst(),
    })
    stage_redis_op(db, RedisOp(
        kind="setex", key=_redis_key(token_hash_hex),
        ttl_seconds=REDIS_TTL_SECONDS,
        value=new_payload.model_dump_json(),
    ))
    return new_payload

async def revoke_token(db: AsyncSession, token: str) -> None:
    th = token_hash(token)
    await db.execute(update(AuthToken).where(
        AuthToken.token_hash == th, AuthToken.revoked_at.is_(None),
    ).values(revoked_at=datetime.now(timezone.utc)))
    stage_redis_op(db, RedisOp(kind="delete", key=_redis_key(th)))

async def revoke_all_active_tokens(db: AsyncSession, user_id: uuid.UUID) -> int:
    hashes = list((await db.execute(
        select(AuthToken.token_hash).where(
            AuthToken.user_id == user_id, AuthToken.revoked_at.is_(None),
        )
    )).scalars().all())
    if not hashes:
        return 0
    await db.execute(update(AuthToken).where(
        AuthToken.user_id == user_id, AuthToken.revoked_at.is_(None),
    ).values(revoked_at=datetime.now(timezone.utc)))
    for th in hashes:
        stage_redis_op(db, RedisOp(kind="delete", key=_redis_key(th)))
    return len(hashes)
```

**验证**：`pytest tests/test_tokens.py -v` 全绿

**提交**：`feat(auth): implement opaque token with stage+commit_with_redis`

#### 阶段 C — 护栏与测试补充

> 封装形态已在阶段 Pre / 阶段 B 直接落地，本阶段只添加静态护栏 + `redis_ops` 模块的独立测试。
> 

**~~Step 3C-1**：新建 `app/auth/redis_ops.py`将 RedisOp + staging + commit 封装在唯一入口里：~~ 已在阶段 Pre 完成。旧代码样例已迁移，省略。

**~~Step 3C-2**：`tokens.py` / `bind.py` 的写函数改为 **只操作 DB + stage 一条 RedisOp~~** 已并入阶段 B。`bind.py` 的 `peek_bind_token` + `stage_consume_bind_token` 见 Step 7 阶段 B。

**~~Step 3C-3**：所有业务调用点统一用 `commit_with_redis`~~ 已在 Step 4 / 6 / 7 / 8 的阶段 B 里直接以最终签名给出，本章不再罗列。

**静态护栏**（防止有人手滑直接 `await db.commit()` 绕开封装）：

- `tests/conftest.py` 的 `db_session` fixture teardown 先断言：
    
    ```python
    assert not session.info.get("pending_redis_ops"), (
        "pending redis ops not flushed — use commit_with_redis() instead of db.commit()"
    )
    ```
    
    若任何测试里有调用则 `stage_redis_op` 后未走 `commit_with_redis` 就 teardown，fixture 报错失败。
    
- `ruff` 自定义规则或 review checklist：`app/api/**` / `app/scripts/**` 禁止直接写 `await db.commit()`（只允许 `commit_with_redis`）。加到 `CLAUDE.md` 的 "Critical Gotchas" 章节。

**测试补充**：

- `tests/test_redis_ops.py`：
    - `commit_with_redis` happy path：stage 一条 setex + 一条 delete → 调用 → DB commit、Redis 有 setex 当前 key 和 delete 旧 key、[session.info](http://session.info) 空
    - DB commit 报错：monkeypatch `db.commit` 招 `RuntimeError` → `commit_with_redis` 传递异常 → [session.info](http://session.info) 的 ops 已被 pop → Redis 无残留
    - Redis flush 报错：monkeypatch `redis.pipeline` 招 → commit_with_redis 不抛（log error） → DB 已提交
    - `discard_pending_redis_ops(db)` 清空 [session.info](http://session.info)
- `test_tokens.py` / `test_login_api.py` / `test_child_bind.py`：保留 happy path；teardown 断言自动拦住漏 commit_with_redis 的路径。

**提交**：`test(auth): add redis_ops safeguards and commit_with_redis tests`

---

### Step 4：FastAPI 鉴权依赖（TDD）

`depends_on: [3]`

**阶段 A（Red）** — `app/auth/deps.py`：

```python
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_client import get_redis
from app.auth.tokens import resolve_token
from app.db import get_db  # 已有（M1 引入）
from app.models.enums import UserRole
from app.schemas.accounts import CurrentAccount

async def get_current_account(
    authorization: Annotated[str | None, Header()] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = ...,  # type: ignore[assignment]
    redis: Annotated[Redis, Depends(get_redis)] = ...,  # type: ignore[assignment]
) -> CurrentAccount:
    """从 Authorization: Bearer 解析 token 并返回当前账号。"""
    raise NotImplementedError

async def require_parent(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
) -> CurrentAccount:
    """断言 role==parent；否则 403。"""
    raise NotImplementedError

async def require_child(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
) -> CurrentAccount:
    """断言 role==child；否则 403。"""
    raise NotImplementedError
```

- [ ]  `tests/test_auth_deps.py` 覆盖：
    - 无 Authorization header → 401
    - Authorization header 不是 `Bearer xxx` 格式 → 401
    - 合法 token 但 resolve_token 返 None → 401
    - 合法 token + X-Device-Id 匹配 → 返 CurrentAccount，role / family_id 正确
    - **合法 token + 缺 X-Device-Id header → 401 `device_changed` + `auth_tokens.revoked_at` 已写入 + Redis key 被清**
    - **合法 token + X-Device-Id 与 payload.device_id 不匹配 → 401 `device_changed` + token 被吊销**
    - **device_changed 后用原 device_id 重放同 token 请求 → 401**（验证吊销路径彻底：Redis miss + DB revoked → 401）
    - `require_parent` 拿到 child → 403
    - `require_child` 拿到 parent → 403
    - **每日首次续期**：parent token mock 明日 → 调 `GET /api/v1/me` → DB `expires_at` 已续 7 天 + Redis payload `last_rolled_date=今日`；同日再调 → DB 不再 UPDATE（只刷 Redis TTL）
    - **续期 DB 落盘**：mock 明日 → /me 触发续期后，**用独立 AsyncSession 绕开外层 savepoint** 直接查 `auth_tokens.expires_at` → 读到续期后的值（验证 `commit_with_redis` 内部 `db.commit()` 实际写入外层 transaction）
    - **子 token 跳过续期**：child token 跨日调 /me → DB `expires_at` 保持 NULL，无 UPDATE；`needs_roll` 对子 token payload 永远返 False
- [ ]  提交：`test(auth): add failing tests for auth deps`

**阶段 B（Green）** — 最小实现：

```python
async def get_current_account(
    authorization: Annotated[str | None, Header()] = None,
    x_device_id: Annotated[str | None, Header(alias="X-Device-Id")] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = ...,  # type: ignore
    redis: Annotated[Redis, Depends(get_redis)] = ...,  # type: ignore
) -> CurrentAccount:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    payload = await resolve_token(db, redis, token)
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    # 设备绑定：比对 X-Device-Id header 与 auth_tokens.device_id
    # 不匹配 → 立即吊销 token + 401（防止 token 泄漏后换设备继续用）
    if x_device_id is None or x_device_id != payload.device_id:
        await revoke_token(db, token)
        await commit_with_redis(db, redis)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "device_changed")
    # 每日首次续期（parent 生效；子 token expires_at=None 时 needs_roll 永远 False）
    if needs_roll(payload):
        payload = await roll_token_expiry(
            db, token_hash_hex=token_hash(token), payload=payload,
        )
        await commit_with_redis(db, redis)
    return CurrentAccount(
        id=payload.user_id,
        role=payload.role,
        family_id=payload.family_id,
        expires_at=payload.expires_at,
    )

async def require_parent(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
) -> CurrentAccount:
    if current.role != UserRole.parent:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "parent required")
    return current

async def require_child(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
) -> CurrentAccount:
    if current.role != UserRole.child:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "child required")
    return current
```

**验证**：`pytest tests/test_auth_deps.py -v` 全绿；手动用 httpx `.post("/protected", headers={"Authorization": f"Bearer {token}"})` 能正确拿到账号。

**提交**：`feat(auth): add FastAPI deps for current account and role guards`

---

### Step 5：Redis 连接池 lifespan

`depends_on: [0]`

- [ ]  `app/auth/redis_client.py`：`get_redis() -> Redis` 工厂 + `main.py` lifespan 建池/关池
- [ ]  `app/config.py` 新增 `redis_url: str`（默认 `redis://redis:6379/0`，沿用 compose 名）
- [ ]  可与 Step 3 的 fakeredis 并行开发；正式环境走真 redis 连接池

```python
# app/auth/redis_client.py
from contextlib import asynccontextmanager
from typing import AsyncIterator

from redis.asyncio import Redis

from app.config import settings

_redis: Redis | None = None

@asynccontextmanager
async def redis_lifespan() -> AsyncIterator[None]:
    """挂到 FastAPI lifespan：创建连接池 / 关闭。"""
    global _redis
    _redis = Redis.from_url(
        settings.redis_url, encoding="utf-8", decode_responses=True,
    )
    try:
        yield
    finally:
        await _redis.aclose()
        _redis = None

async def get_redis() -> Redis:
    """FastAPI Depends 工厂。"""
    assert _redis is not None, "redis pool not initialized (check lifespan)"
    return _redis
```

- [ ]  `main.py` 把 `create_app()` 改用 `lifespan=...` 入参，包住 `redis_lifespan()`

**验证**：启动 `docker compose up api` 日志无 Redis 连接错误；`docker compose exec api python -c "import asyncio; from app.auth.redis_client import *; ..."` 能手动 ping 通

**提交**：`feat(auth): wire redis connection lifespan for auth token cache`

---

### Step 6：Login API + Logout API（TDD）

`depends_on: [2, 3, 4, 5]`

**阶段 A（Red）** — `app/api/auth.py` 入口签名：

```python
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    """父账号登录：phone + password → opaque token。"""
    raise NotImplementedError

@router.post("/logout", status_code=204)
async def logout(
    authorization: Annotated[str, Header()],
    current: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """主动下线当前父账号 token。限 parent（child 语义见决策背景页 §4.1）。"""
    raise NotImplementedError
```

- [ ]  `tests/test_login_api.py`：
    - happy path：种一个 parent（`User` + `password_hash = hash_password("abcdefgh")`）→ POST /login → 返回 token + AccountOut；AccountOut 不含 `admin_note` / `password_hash`
    - **response JSON 文本断言**：`assert "password_hash" not in resp.text and "admin_note" not in resp.text`（防未来 schema 泄漏）
    - **device_id 贯穿到 DB**：登录后查 `auth_tokens` 表该行 → `device_id == LoginRequest.device_id`
    - **device_id 贯穿到 Redis**：从 Redis 读 `auth:{sha256(token)}` → 反序列化 payload → `payload.device_id == LoginRequest.device_id`
    - **新 token 立即可用**：用 response 里的 token + 同一 device_id 调 `GET /api/v1/me` → 200（回归 commit 顺序，DB 与 Redis 一致）
    - **LoginRequest 缺 device_id** → 422（pydantic 校验）
    - 错误密码 → 401，不区分「账号不存在」vs「密码错」（防枚举）
    - 错误 phone → 401
    - 非活跃账号（is_active=false）→ 401
    - child 账号拿 phone 来登录 → 401（child 本就没 password_hash）
    - **同 parent 二次登录（新 device_id）→ 老 token 失效**：先 login 拿 token_A（dev_A）→ 再 login 拿 token_B（dev_B）→ 用 token_A + dev_A 调 `GET /api/v1/me` → 401；`auth_tokens` 表 token_A 行 `revoked_at IS NOT NULL`；Redis 里 token_A 的 key 被清
    - **同 parent 同设备复登**：revoke_all 也会吊销「自己上次的 token」，用老 token 调 /me → 401（接受这个副作用：语义干净优先）
    - logout happy path → 204 + 再用老 token 请求 → 401
    - **logout 幂等**：同一 token 调两次 logout → 第二次 401（token 已吊销；require_parent 先拦）
    - **logout 需 parent**：用 child token 调 /logout → 403
- [ ]  提交：`test(auth): add failing tests for login/logout endpoints`

**阶段 B（Green）** 实现要点：

```python
@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    # 统一 401，不区分账号不存在 / 密码错（防枚举）
    stmt = select(User).where(
        User.phone == payload.phone,
        User.role == UserRole.parent,
        User.is_active.is_(True),
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or user.password_hash is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not verify_password(user.password_hash, payload.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    # 新设备登录吊销该 parent 所有活跃 token（一次一设备；同设备复登副作用见决策背景页 §4.2）
    await revoke_all_active_tokens(db, user.id)
    token = await issue_token(
        db,
        user_id=user.id, role=user.role, family_id=user.family_id,
        device_id=payload.device_id,
        ttl_days=7,
    )
    await commit_with_redis(db, redis)
    return LoginResponse(
        token=token,
        account=AccountOut(
            id=user.id, role=user.role, family_id=user.family_id,
            phone=user.phone, is_active=user.is_active,
        ),
    )

@router.post("/logout", status_code=204)
async def logout(
    authorization: Annotated[str, Header()],
    current: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    # 抵达 handler 时 require_parent 已验证 Authorization 存在 + token 合法 + role=parent
    token = authorization.split(" ", 1)[1].strip()
    await revoke_token(db, token)
    await commit_with_redis(db, redis)
```

- [ ]  `main.py` 挂 `router`

#### N3 · GET /api/v1/me 端点（登入状态代理可用的基本确认接口）

`app/api/me.py`（新建独立路由，prefix 在 `/api/v1`，避免被 `/auth` 前缀掉到 `/api/v1/auth/me`）：

```python
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_account
from app.db import get_db
from app.models.accounts import User
from app.schemas.accounts import AccountOut, CurrentAccount

router = APIRouter(prefix="/api/v1", tags=["me"])

@router.get("/me", response_model=AccountOut)
async def get_me(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountOut:
    user = await db.get(User, current.id)
    if user is None:
        raise HTTPException(404, "user not found")
    return AccountOut(
        id=user.id, role=user.role, family_id=user.family_id,
        phone=user.phone, is_active=user.is_active,
    )
```

- [ ]  `main.py` 挂 `me.router`（与 `auth.router` / `children.router` 并列）
- [ ]  `tests/test_me_api.py`：parent / child 各拿 token 调 `/api/v1/me` 返 AccountOut、无 token → 401、吊销 token → 401、设备不匹配 → 401 `device_changed`
- [ ]  `tests/test_e2e_auth.py` 已引用 `/api/v1/me`，不需重写

#### N4 · 登录端的 rate limit（OR 双维度；phone 5/分 + IP 20/分）

> 固定窗口 60 秒 + `nx=True` TTL（避免 `INCR` 每次刷新 TTL 导致永不解锁）。任一维度超限即 429；超限后不显示锁定，窗口过期自愈。IP 来源 MVP 用 `request.client.host`；P0 上 SLB 后切 `X-Forwarded-For` 最左元素（附加逻辑记入待办）。详见 [M4 · 决策背景与避坑记录](https://www.notion.so/M4-7d37f77eaeeb49b388c96dc72140beac?pvs=21) §10。
> 

在 `app/api/auth.py` login handler 最前面（argon2 verify 之前）插入：

```python
from fastapi import Request

LOGIN_PHONE_LIMIT = 5
LOGIN_IP_LIMIT = 20
LOGIN_WINDOW_SECONDS = 60

async def _check_and_incr_login_limit(redis: Redis, phone: str, ip: str) -> None:
    phone_key = f"login_fail:phone:{phone}"
    ip_key = f"login_fail:ip:{ip}"
    phone_count = int(await redis.get(phone_key) or 0)
    ip_count = int(await redis.get(ip_key) or 0)
    if phone_count >= LOGIN_PHONE_LIMIT or ip_count >= LOGIN_IP_LIMIT:
        raise HTTPException(429, "too many attempts; try again later")

async def _incr_login_fail(redis: Redis, phone: str, ip: str) -> None:
    async with redis.pipeline(transaction=False) as pipe:
        pipe.incr(f"login_fail:phone:{phone}")
        pipe.expire(f"login_fail:phone:{phone}", LOGIN_WINDOW_SECONDS, nx=True)
        pipe.incr(f"login_fail:ip:{ip}")
        pipe.expire(f"login_fail:ip:{ip}", LOGIN_WINDOW_SECONDS, nx=True)
        await pipe.execute()

@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,                    # 新增，用于取 client.host
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    client_ip = request.client.host if request.client else "unknown"
    await _check_and_incr_login_limit(redis, payload.phone, client_ip)

    # …… 原有 SELECT user / verify_password 逻辑 ……
    if user is None or user.password_hash is None or not verify_password(user.password_hash, payload.password):
        await _incr_login_fail(redis, payload.phone, client_ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    # 成功的 UX 小细节：清零计数，避免先前失败试的残留影响合法登录（并不影响安全性）
    # 走 staging，随下方 issue_token 的 stage 一起在 commit_with_redis 中 flush；
    # 若 DB commit 回滚，这两条 delete 自动丢弃，计数保留，语义一致
    stage_redis_op(db, RedisOp(kind="delete", key=f"login_fail:phone:{payload.phone}"))
    stage_redis_op(db, RedisOp(kind="delete", key=f"login_fail:ip:{client_ip}"))

    # …… 原有 revoke_all_active_tokens + issue_token 之后统一 `await commit_with_redis(db, redis)`
```

**测试补充**（`tests/test_login_api.py`）：

- 同 phone 连续错密码 5 次 → 第 6 次返 429；正确密码此时也被 429（已达限）→ 等 60 秒 → mock 推时间后正确密码 200
- 同 IP 连续 20 次错密码（跨 phone）→ 第 21 次 429
- 成功登录后 Redis 中两个计数 key 被清
- `expire nx=True`：连续 INCR 5 次后查 `TTL login_fail:phone:*` → 应 ≤ 60 且 ≠ -1（证明没被每次 INCR 刷新）

**验证**：`pytest tests/test_login_api.py -v` 全绿；curl 手测 happy path 返回 token 可用于访问 `GET /api/v1/me`

**提交**：`feat(api): add /auth/login and /auth/logout endpoints`

**提交**：`feat(api): add GET /api/v1/me endpoint`

**提交**：`feat(auth): add per-phone and per-ip rate limit on login`

---

### Step 7：子账号创建 + bind_token + 兑换（TDD）

`depends_on: [3, 4, 5]`

**阶段 A（Red）** — `app/auth/bind.py` + `app/api/children.py` 签名：

```python
# app/auth/bind.py
BIND_KEY_PREFIX = "bind:"
BIND_TTL_SECONDS = 300

async def issue_bind_token(
    redis: Redis, *, parent_user_id: uuid.UUID, child_user_id: uuid.UUID,
) -> str:
    """生成一次性绑定 token。5 分钟 TTL。纯 Redis 写（无 DB 同步需求）。"""
    raise NotImplementedError

async def peek_bind_token(
    redis: Redis, bind_token: str,
) -> Optional[tuple[uuid.UUID, uuid.UUID]]:
    """只 GET 不删。调用方在 DB 写入完成后 stage_consume_bind_token 入 staging，
    由 commit_with_redis 统一 flush。返回 (parent_user_id, child_user_id) 或 None。"""
    raise NotImplementedError

def stage_consume_bind_token(db: AsyncSession, bind_token: str) -> None:
    """把 bind_token 的 Redis delete 入 staging；调用方紧跟 commit_with_redis。"""
    raise NotImplementedError
```

```python
# app/api/children.py
router = APIRouter(prefix="/api/v1", tags=["children"])

@router.post("/children", response_model=AccountOut, status_code=201)
async def create_child(
    payload: CreateChildRequest,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountOut:
    """父账号创建一个子账号：users(role=child) + child_profiles。"""
    raise NotImplementedError

@router.post("/children/{child_user_id}/bind-token", response_model=BindTokenResponse)
async def create_bind_token(
    child_user_id: uuid.UUID,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> BindTokenResponse:
    """为指定子账号生成一次性绑定 token。"""
    raise NotImplementedError

# app/api/children.py：父端 QR 页面轮询子端扫码状态（挂在现有 children router / prefix=/api/v1，无需新文件）
@router.get("/bind-tokens/{bind_token}/status", response_model=BindTokenStatusOut)
async def get_bind_token_status(
    bind_token: str,
    redis: Annotated[Redis, Depends(get_redis)],
) -> BindTokenStatusOut:
    """轮询 bind_result:{bind_token} Redis key；无结果再看 bind_token 本身是否活着。

    **不鉴权**：bind_token 本身是一次性机密凭证（5min TTL + 16 字节 urlsafe 随机），
    持有即算父端——与「生成 bind_token」端点的 require_parent 对称闭合（生成端防未授
    权创建，查询端只需 bind_token 凭证）。详见决策背景 §1.2。
    """
    raise NotImplementedError

# app/api/auth.py 末尾增加
@router.post("/redeem-bind-token", response_model=LoginResponse)
async def redeem_bind_token_endpoint(
    payload: RedeemBindTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    """子端扫码后换取永久 child token；同时吊销该 child 所有老 token。"""
    raise NotImplementedError

# app/api/children.py：父端「下线所有设备」按钮（定位见决策背景页 §4.3）
@router.post("/children/{child_user_id}/revoke-tokens", status_code=204)
async def revoke_child_tokens(
    child_user_id: uuid.UUID,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """吊销指定 child 的全部活跃 token。家庭边界：parent 只能下线本 family 的 child。"""
    raise NotImplementedError
```

- [ ]  `tests/test_child_bind.py` 覆盖：
    - parent 创建 child → 数据库中 users(role=child, family_id=[parent.family](http://parent.family)_id) + child_profiles 各一行；AccountOut 返回正确
    - child 账号调 /children → 403（require_parent 拦截）
    - parent A 为 family A 的 child 生成 bind_token OK；parent B（不同 family）为同一 child 生成 → 404（家庭边界）
    - 正常兑换 → 返回永久 child token（`expires_at IS NULL`）；bind_token 立即失效
    - 重复兑换同一 bind_token → 400 / 404
    - 5 分钟后兑换（fakeredis 推时间）→ 400 / 404
    - **child 换设备 redeem 吊销老 token**：child 先 redeem 拿 token_A（dev_A）→ parent 再生成新 bind_token → child 新设备 redeem 拿 token_B（dev_B）→ 用 token_A + dev_A 调 `GET /api/v1/me` → 401；`auth_tokens` token_A 行 `revoked_at IS NOT NULL`；Redis 里 token_A 的 key 已清
    - **POST /children/{id}/revoke-tokens happy path**：parent 调 → 204；child 所有活跃 token 全部 `revoked_at IS NOT NULL` + Redis 全清；child 持老 token 调 /me → 401
    - **revoke-tokens 家庭边界**：parent B（不同 family）调 family A 的 child revoke-tokens → 404（不泄漏 child 是否存在）
    - **revoke-tokens require_parent**：child 用 child token 调 revoke-tokens → 403
    - **下线后重新上线闭合**：revoke-tokens 后 parent 再生成新 bind_token → child 扫码 → 新 token 可用（验证「下线 → 生成绑定码 → 扫码」路径完整）
    - **GET /bind-tokens/{tok}/status 未扫**：parent 生成 bind_token 后立即 GET → `status="pending"`，child_user_id / bound_at 均为 None
    - **GET /bind-tokens/{tok}/status 已扫**：子端 redeem 成功后父端 GET → `status="bound"` + 正确的 child_user_id + 有值 bound_at（父端轮询闭环）
    - **GET /bind-tokens/{tok}/status 过期未扫**：fakeredis 推时间越过 300s 且未 redeem → GET → 404 `bind token not found or expired`
    - **GET /bind-tokens/{tok}/status 过期已扫**：子端 redeem 后推时间 400s（超过 bind_token 300s TTL 但未到 bind_result 600s TTL）→ 父端 GET 仍得 `status="bound"`（验证容忍父端网络抖动）
    - **GET /bind-tokens/{tok}/status 新旧互不干扰**：parent 生成 tok_A → 未扫 → 刷新生成 tok_B → 子端扫 tok_B → GET tok_A → `status="pending"`（或 404 若已过期）；GET tok_B → `status="bound"`
    - **GET /bind-tokens/{tok}/status 不打 DB**：monkeypatch `db.execute` 抛异常依然返正常结果（验证实现路径纯 Redis）
    - **GET /bind-tokens/{tok}/status 无鉴权**：不传 Authorization / X-Device-Id 亦能正常返回（验证与生成端的不对称，符合设计）
- [ ]  提交：`test(auth): add failing tests for child creation and qr binding`

**阶段 B（Green）** 关键实现：

```python
# app/auth/bind.py
import json
import secrets
import uuid
from typing import Optional

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_ops import RedisOp, stage_redis_op

BIND_KEY_PREFIX = "bind:"
BIND_TTL_SECONDS = 300

async def issue_bind_token(
    redis: Redis, *, parent_user_id: uuid.UUID, child_user_id: uuid.UUID,
) -> str:
    """签一次性 bind_token。纯 Redis 写（无 DB 变更需共同 commit），
    直接 setex，不进 staging。"""
    token = secrets.token_urlsafe(16)
    await redis.setex(
        f"{BIND_KEY_PREFIX}{token}",
        BIND_TTL_SECONDS,
        json.dumps({
            "parent_user_id": str(parent_user_id),
            "child_user_id": str(child_user_id),
        }),
    )
    return token

async def peek_bind_token(
    redis: Redis, bind_token: str,
) -> Optional[tuple[uuid.UUID, uuid.UUID]]:
    """只 GET 不删。后续 DB 写入 + stage_consume_bind_token + commit_with_redis
    完成后才真正把 bind_token 从 Redis 删掉，DB 回滚则自动放弃删除（bind_token
    还在 5min TTL 内可重试）。"""
    raw = await redis.get(f"{BIND_KEY_PREFIX}{bind_token}")
    if raw is None:
        return None
    data = json.loads(raw)
    return uuid.UUID(data["parent_user_id"]), uuid.UUID(data["child_user_id"])

def stage_consume_bind_token(db: AsyncSession, bind_token: str) -> None:
    stage_redis_op(db, RedisOp(kind="delete", key=f"{BIND_KEY_PREFIX}{bind_token}"))

# “扫码成功”事实写入：供父端轮询 GET /bind-tokens/{tok}/status 读。
# TTL 比 bind_token 本身的 300s 长：容忍父端网络抖动 / 页面重载后还能查到结果。
BIND_RESULT_KEY_PREFIX = "bind_result:"
BIND_RESULT_TTL_SECONDS = 600

def stage_record_bind_result(
    db: AsyncSession,
    bind_token: str,
    child_user_id: uuid.UUID,
) -> None:
    """把「bind_token 已成功兑换」的事实 stage 到 session.info，由 commit_with_redis
    和 DB 写入原子地决定落不落——DB 回滚则此条也不 flush，父端继续看到 pending。"""
    stage_redis_op(db, RedisOp(
        kind="setex",
        key=f"{BIND_RESULT_KEY_PREFIX}{bind_token}",
        ttl_seconds=BIND_RESULT_TTL_SECONDS,
        value=json.dumps({
            "child_user_id": str(child_user_id),
            "bound_at": datetime.now(timezone.utc).isoformat(),
        }),
    ))
```

```python
# app/api/children.py 实现关键点
@router.post("/children", response_model=AccountOut, status_code=201)
async def create_child(
    payload: CreateChildRequest,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> AccountOut:
    child = User(
        family_id=parent.family_id, role=UserRole.child, phone=None, is_active=True,
    )
    db.add(child)
    await db.flush()  # 拿 child.id
    db.add(ChildProfile(
        child_user_id=child.id, created_by=parent.id,
        birth_date=payload.birth_date, gender=payload.gender,
    ))
    # 无 Redis ops 也走统一入口：符合 app/api/** 护栏「禁止裸 await db.commit()」
    await commit_with_redis(db, redis)
    return AccountOut(
        id=child.id, role=child.role, family_id=child.family_id,
        phone=None, is_active=True,
    )

@router.post("/children/{child_user_id}/bind-token", response_model=BindTokenResponse)
async def create_bind_token(
    child_user_id: uuid.UUID,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> BindTokenResponse:
    # 家庭边界检查
    stmt = select(User).where(
        User.id == child_user_id,
        User.role == UserRole.child,
        User.family_id == parent.family_id,
        User.is_active.is_(True),
    )
    child = (await db.execute(stmt)).scalar_one_or_none()
    if child is None:
        raise HTTPException(404, "child not found in family")
    token = await issue_bind_token(
        redis, parent_user_id=parent.id, child_user_id=child.id,
    )
    return BindTokenResponse(bind_token=token)

@router.get("/bind-tokens/{bind_token}/status", response_model=BindTokenStatusOut)
async def get_bind_token_status(
    bind_token: str,
    redis: Annotated[Redis, Depends(get_redis)],
) -> BindTokenStatusOut:
    # 1) 已兑换 → status=bound
    result_raw = await redis.get(f"{BIND_RESULT_KEY_PREFIX}{bind_token}")
    if result_raw is not None:
        data = json.loads(result_raw)
        return BindTokenStatusOut(
            status="bound",
            child_user_id=uuid.UUID(data["child_user_id"]),
            bound_at=datetime.fromisoformat(data["bound_at"]),
        )
    # 2) 未兑换但 bind_token 还活着 → status=pending
    if await redis.exists(f"{BIND_KEY_PREFIX}{bind_token}"):
        return BindTokenStatusOut(status="pending")
    # 3) 两者皆无 → bind_token 已过期且未兑换（或根本不存在）
    raise HTTPException(404, "bind token not found or expired")

# app/api/auth.py
@router.post("/redeem-bind-token", response_model=LoginResponse)
async def redeem_bind_token_endpoint(
    payload: RedeemBindTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    # peek 不删；DB 写入成功后 stage_consume_bind_token 入 staging，
    # 由 commit_with_redis 统一 flush（DB 回滚则 bind_token 保留，5min TTL 内可重试）
    peeked = await peek_bind_token(redis, payload.bind_token)
    if peeked is None:
        raise HTTPException(400, "bind token invalid or expired")
    _parent_id, child_id = peeked
    child = await db.get(User, child_id)
    if child is None or not child.is_active or child.role != UserRole.child:
        raise HTTPException(400, "child account unavailable")

    # 新设备扫码吊销该 child 所有活跃 token（与 /auth/login 对齐；双清原则见决策背景页 §4.4）
    await revoke_all_active_tokens(db, child.id)
    token = await issue_token(
        db,
        user_id=child.id, role=child.role, family_id=child.family_id,
        ttl_days=None,  # 永不过期
        device_id=payload.device_id,
        device_info=payload.device_info,
    )
    stage_consume_bind_token(db, payload.bind_token)
    # 同时 stage 一条 bind_result 供父端轮询端点读；DB 回滚则两条同时丢，父端保持看到 pending
    stage_record_bind_result(db, payload.bind_token, child.id)
    await commit_with_redis(db, redis)
    return LoginResponse(
        token=token,
        account=AccountOut(
            id=child.id, role=child.role, family_id=child.family_id,
            phone=None, is_active=True,
        ),
    )
```

**验证**：`pytest tests/test_child_bind.py -v` 全绿；端到端 curl：parent 登录 → 创建 child → 生成 bind_token → 另起一个客户端 POST /redeem-bind-token → 拿到永久 child token

**提交**：`feat(api): add child account creation and qr binding flow`

---

### Step 8：运维 CLI — create_parent / reset_parent_password

`depends_on: [2, 6]`

- [ ]  `app/scripts/_common.py`：共享 `ArgParser` 骨架 + async runner（`asyncio.run(main())`）+ `cli_runtime()` context manager 统一为脚本提供 `(AsyncSession, Redis)`。CLI 不走 FastAPI lifespan，不能复用 `app.auth.redis_client._redis` 全局（`get_redis()` 会 assert 失败），**必须本地建池**（详见决策背景 §10.3）：

```python
# app/scripts/_common.py
from contextlib import asynccontextmanager
from typing import AsyncIterator

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from app.config import settings

@asynccontextmanager
async def cli_runtime() -> AsyncIterator[tuple[AsyncSession, Redis]]:
    """CLI 专用：手动建 (AsyncSession, Redis)；退出时一并释放。
    decode_responses=True 必须与生产 redis_client 保持一致，
    否则 commit_with_redis flush 后 resolve_token 回填路径会遇 bytes。"""
    engine = create_async_engine(settings.database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    session = session_maker()
    try:
        yield session, redis
    finally:
        await session.close()
        await redis.aclose()
        await engine.dispose()
```

- [ ]  `create_parent.py` / `reset_parent_password.py` 都走 `async with cli_runtime() as (db, redis): ...`；reset 脚本在 UPDATE password_hash 后调 `await revoke_all_active_tokens(db, user.id)` 再 `await commit_with_redis(db, redis)` 完成 DB + Redis 双清
- [ ]  `app/scripts/create_parent.py`：
    - `--note "..."` 必填参数
    - 流程：新建 `families` → 新建 `users(role=parent, is_active=true, phone=generate_phone(), password_hash=hash_password(pw), admin_note=note)`（若 phone 撞已有 parent，重试最多 10 次）→ 新建 `family_members(family_id, user_id, role=parent)`
    - commit 后**控制台打印**：
    
    ```
    ✅ parent created
       phone:    kxmp
       password: gvxkzbqm
       user_id:  a1b2c3d4-...
       note:     张三-家长
    ⚠️  明文密码仅此一次打印，请立即妥善保管。
    ```
    
- [ ]  `app/scripts/reset_parent_password.py`：
    - `--phone xxxx` 必填
    - 流程：查活跃 parent → 新 password = `generate_password()` → `password_hash = hash_password(pw)` → `update(User)` → **调 `await revoke_all_active_tokens(db, user.id)` 后 `await commit_with_redis(db, redis)`**（DB `revoked_at` + Redis 双清，复用 Step 3 封装；走统一入口不裸 `db.commit()`）→ 控制台打印新密码
    - 安全：fail closed，如 phone 未找到 → 非 0 退出码 + stderr
- [ ]  `tests/test_scripts.py`：用 `subprocess` / 直接 import `main()` 函数做冒烟；断言 DB 落地 + stdout 含预期 phone/password 格式

**验证**：

```bash
# create
docker compose exec api python -m app.scripts.create_parent --note "张三-家长 · 内测批次 1"
# → 控制台打印 phone + password；DB users 表多一行 role=parent + admin_note 对应；family_members 多一行

# reset
docker compose exec api python -m app.scripts.reset_parent_password --phone <上一步的 phone>
# → 控制台打印新密码；老 token 立即失效（curl 测）
```

**提交**：`feat(scripts): add create_parent and reset_parent_password CLIs`

---

### Step 9：端到端集成测试

`depends_on: [7, 8]`

- [ ]  `tests/test_e2e_auth.py` 一条完整剧本：
    1. `create_parent` CLI → 种出 parent
    2. POST `/auth/login` 用种出的 phone/password → 拿 parent token
    3. GET `/api/v1/me` 带 parent token → 返 parent AccountOut
    4. POST `/children` → 建 child
    5. POST `/children/{id}/bind-token` → 拿 bind_token
    6. POST `/auth/redeem-bind-token` → 拿 child token（永不过期）
    7. GET `/api/v1/me` 带 child token → 返 child AccountOut
    8. GET `/api/v1/children/xxx/bind-token` 带 child token → 403（require_parent）
    9. POST `/auth/logout` 带 parent token → 204
    10. 老 parent token → 401
    11. `reset_parent_password` CLI → parent 所有 token 失效
    12. 新密码登录 → 新 token OK
- [ ]  `ruff check .` / `ruff format .` / `basedpyright app` 全绿

**验证**：`pytest tests/test_e2e_auth.py -v` 全绿

**提交**：`test(auth): add end-to-end scenario covering full M4 flow`

---

### Step 10：[CLAUDE.md](http://CLAUDE.md) 同步

`depends_on: [9]`

- [ ]  更新仓库 `CLAUDE.md`：
    - Current milestone 改为 M4 完成
    - Architecture Backend Structure 加入 `app/auth/` + `app/scripts/` + `app/api/auth.py` + `app/api/children.py` + `app/api/me.py`
    - 新增 "Auth" 小节：Opaque token + Redis cache + argon2id + CLI 种子 + `stage_redis_op` / `commit_with_redis` 封装原则
    - "Critical Gotchas" 加：
        - `app/api/**` / `app/scripts/**` 禁止裸 `await db.commit()`，统一走 `commit_with_redis`（redis_ops 封装）
        - CLI 脚本必须走 `app/scripts/_common.py` 的 `cli_runtime()`，不能复用 FastAPI 的 `_redis` 全局
        - `argon2-cffi` 在 Python 3.14 Docker 内如 Step 0 实测需 build-essential，则 Dockerfile 加对应 apt 安装

**验证**：手动阅 `CLAUDE.md` diff 无语义冲突

**提交**：`docs(claude): sync CLAUDE.md for M4 auth stack`

> Notion 侧文档（Agent 指引 §2.3 / [](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21) / `M4 执行偏差记录` 子页）**不在本计划 Step 范围**；按「Notion 文档相关更新根据每步实施报告触发」的规则，由 `/step-execute` 完成每个 Step 后自行回写。
> 

---

## 验收矩阵

| 验收项 | 通过标准 |
| --- | --- |
| 父账号登录 | 种子 parent 用 CLI 打印的 phone/password → POST /auth/login → 返 token 可用 |
| 主动吊销即时生效 | logout 后 0 延迟 401；reset_parent_password 后所有旧 token 即时 401；父端 POST /children/{id}/revoke-tokens 后 child 所有设备 0 延迟 401 |
| 设备绑定即时生效 | parent 新设备登录 / child 新设备扫码后，老设备持原 token + 原 device_id 调任何端点 → 0 延迟 401（DB `revoked_at`  • Redis 双清）；同 token 换 X-Device-Id header 重放 → 401 `device_changed` |
| 子账号绑定一次性 | bind_token 兑换一次后再兑换 → 400；5 分钟后 → 400 |
| 响应屏蔽 | 所有 `/api/v1/*` 返回的账号结构体中**不含** `admin_note` / `password_hash` |
| CLI 明文密码仅打印一次 | 重跑 `create_parent` 不会再显示历史密码；DB 只存 hash |
| 登录 rate limit | 同 phone 连续错密码 5 次后第 6 次返 429；同 IP（跨 phone）累计 20 次后第 21 次 429；60 秒窗口过后自愈；成功登录清零两个计数器 |
| Redis/DB 顺序一致性 | 所有写入 Redis 的业务函数（issue_token / revoke_token / revoke_all_active_tokens / roll_token_expiry / peek_bind_token + stage_consume_bind_token / 登录 rate-limit 成功清零）都通过 `stage_redis_op`  • `commit_with_redis` 统一入口；monkeypatch `db.commit` 失败的用例下，Redis 无残留；conftest teardown 断言拦住所有漏调 `commit_with_redis` 的路径 |
| 全量测试 | `pytest -v` 全绿；`basedpyright app` 无 error；`ruff check` 无 issue |

---

## M4 延伸清单（不在本里程碑落地 —— 仅交叉参照）

已入 [](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)，由对应阶段前触发：

- **P0 上线前**：切 PNVS 手机号认证（含 MVP 老密码端点下线策略 + 历史账号迁移）/ App 工信部备案 / 账号注销 / 生成式 AI 服务登记 / PIPL 未成年人合规 / 企业开发者账号
- **P1 上线前**：改绑手机号 / **`users.last_login_at` 审计字段**（安全事件追溯 + 不活跃账号清理）
- **~~P3 上线后**：Redis↔DB 双写崩溃窗口统一跟踪~~（已在本里程碑 Step 3C RedisOp pattern 重构在办：issue_token / revoke_token / revoke_all_active_tokens / redeem_bind_token 全部改为 DB commit 后再 flush Redis，不再遗留崩溃窗口。resolve_token 的 cache 续租失败不影响正确性，不列入窗口跟踪）
- **~~P1 上线后：多设备登录 + 挤下线~~**（已因 M4 设备绑定方案 obsolete）
- **P2**：自备短信签名 / 家庭多父邀请码 / 一键登录

---

## 发现与建议

[M4 · 决策背景与避坑记录](https://www.notion.so/M4-7d37f77eaeeb49b388c96dc72140beac?pvs=21)

[M4 执行偏差记录](https://www.notion.so/M4-9341dc6b128f448ea1aeaa733de9ae81?pvs=21)