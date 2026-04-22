# M4-patch · 账号体系测试补强 — 实施计划 (4-patch/17)

<aside>
🎯

**目标**：针对 M4 完成后对 `backend/tests/` 的批判性审阅所发现的漏洞，按 P0–P3 分级一次性补齐。全面应测尽测，不再区分 "M4 范围内外"。

**范围**：整个 `backend/tests/` 的补测与重写（包括 chat / graph / SSE）。不改任何 `backend/app/**` 生产代码。若补测过程中证伪出生产缺陷，停手开 bug 工单，不混入本 patch。

**分支**：`feat/m4-patch-tests`，合并前 rebase 到 `main`。

**执行者**：本页可直接交给执行 agent，所有决策点已在下文锁定。

</aside>

## 📎 背景与依据

- 上游：[M4 · 账号体系 — 实施计划 (4/17)](https://www.notion.so/M4-4-17-580ad7fb03324443ac405eab6c3e00c6?pvs=21)（M4 实施计划）
- 审阅结论：NoNo 对 `backend/tests/` 的批判性比对（2026-04-21 thread）
- 执行偏差记录：[M4 执行偏差记录](https://www.notion.so/M4-9341dc6b128f448ea1aeaa733de9ae81?pvs=21)
- 妥协跟踪：[](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)

## 🧱 总体约束（决策锁定）

- **不走经典 TDD**：生产代码已落地，补测应一次 pass。若 fail → 证伪出生产 bug，停手开独立工单，不塞进本 patch。
- **取代方案：mutation smoke**。每条新测 / 重写测 pass 后，按本页给出的 "mutation smoke" 步骤临时破坏被测生产代码的关键行，确认测试 fail，再恢复代码。防止哑测试。
- **commit 粒度**：每条 P0 独立 commit，`test(m4-patch): <用例名>`。P1 按 B1/B2/B3/B4 分别 commit。P2（Phase C）可合并为 `test(m4-patch): phase-c cleanup`。P3（Phase D）按子模块合并为 3 个 commit：`test(chat): dashscope hardening` / `test(chat): graph hardening` / `test(chat): sse hardening`。
- **fixture 约束**：不新增 session-scope fixture（与 M4 function-scope + NullPool 决策对齐）。复用现有 `db_session` / `engine` / `redis_client` / `app` / `api_client`。
- **工具依赖**：Phase D 的新用例依赖 `pytest` / `pytest-asyncio` / `httpx` / `caplog`（标准库，无需新增依赖）。如要新增其它包，在本页独立记录与决策。
- **时钟推进方法锁定**：
    - fakeredis TTL **不随真实时间流逝**，不要用 `time.sleep` / `freezegun` 模拟 TTL 到期。
    - **统一用 `await redis_client.delete(key)` 模拟 TTL 到期**。A1 / A3 / A4 / A7 均适用。
    - 涉及跨天语义的（仅 B4），用 `monkeypatch.setattr("app.auth.tokens._today_cst", lambda: "2026-04-20")`。
- **跨 connection DB 可见性无法验证**：conftest savepoint 架构下，outer transaction 未 commit，其他 connection 根本看不到本测试的写入。该语义的验证下放到 M5 前的 integration 体系，本 patch **不尝试**。B2 已重新定义为“Redis payload 跨 call 可见性”。

## 📖 事实参考（从源码核对，执行时以此为准）

**账号体系 API 路由**（全部在 `/api/v1` 下）：

- `POST /api/v1/auth/login` · `POST /api/v1/auth/logout` · `POST /api/v1/auth/redeem-bind-token`
- `POST /api/v1/children` · `POST /api/v1/children/{child_user_id}/bind-token`
- `GET  /api/v1/bind-tokens/{bind_token}/status`
- `POST /api/v1/children/{child_user_id}/revoke-tokens`

**chat/SSE 路由**：`POST /api/dev/chat/stream`（定义于 `app/api/dev_chat.py`）。请求体 `DevChatRequest{ message: str, min_length=1, max_length=2000 }`。

**Redis key**：

- `bind:<token>` TTL=300，value = `{parent_user_id, child_user_id}` JSON
- `bind_result:<token>` TTL=600，value = `{child_user_id, bound_at}` JSON
- `auth:<token_hash>` TTL=600，value = `TokenPayload` JSON 字符串（**不是** Redis hash 结构！）
- `login_fail:phone:<phone>` / `login_fail:ip:<ip>` TTL=60（`expire nx=True`）

**关键生产代码签名**：

- `peek_bind_token(redis, token) → Optional[(parent_uuid, child_uuid)]`（纯读，不删）
- `verify_password(hashed, password) → bool`：仅接 `VerifyMismatchError` 返 `False`；其它 argon2 异常（`InvalidHash`、等）**上抛**。这是设计意图，**不要当成 bug**。
- `TokenPayload.last_rolled_date`：ISO date 字符串（Asia/Shanghai），存于 Redis `auth:<th>` 的 JSON value 里。
- `get_bind_token_status(bind_token, redis)`：签名里**不依赖** `AsyncSession`（A5 据此签名锁定）。
- `roll_token_expiry` / `revoke_token` / `revoke_all_active_tokens` / `issue_token` / `stage_consume_bind_token` / `stage_record_bind_result` **都是 stage 模式**，调用方必须 `await commit_with_redis(db, redis)` 才会落盘。

**chat 关键生产行为**：

- `ChatDashScopeQwen._astream`：每个上游 response 毫无条件 yield 一个 `ChatGenerationChunk(AIMessageChunk(content=delta))`（delta 可为空）；仅当 `finish_reason in ("stop","length","tool_calls","content_filter")` 时额外 yield 一个 finish chunk，带 `response_metadata={"finish_reason": ...}` 与 `usage_metadata={input_tokens, output_tokens, total_tokens}`。这行白名单是守门逻辑，D4 锁定。
- `build_chat_graph()`：单节点 `call_main_llm`，`ChatState.messages` 有 `add_messages` reducer（追加语义，非覆盖）。D8 锁定。
- `stream_chat(user_message, session_id)`：事件序列 `start → delta* → end`；上游业务异常→ `start → delta* → error` 且后续不 yield end（有 `return`）；`CancelledError` / `ClientDisconnect` / `BrokenResourceError` 直接 re-raise，不发 error。D9 / D10 / D11 锁定。
- **空 content delta 不向客户端发 delta 帧**：`sse.py` 里 `if chunk is not None and chunk.content` 过滤。

**conftest teardown 护栏**：若测试结束时 `db_session.info["pending_redis_ops"]` 非空则 assert。不 commit_with_redis 的测试必须在结束前 `discard_pending_redis_ops(db_session)` 或 `db_session.rollback()` 并手动 discard。

## 🅰️ Phase A · P0 账号体系补测（必须全做）

### A1 · bind_token 过期后 redeem 失败

- 文件：`test_child_bind.py`
- 用例：`test_redeem_bind_token_returns_400_when_bind_key_expired`
- 步骤：
    1. 现有 fixture 种 parent + child；`await issue_bind_token(redis_client, parent_user_id=..., child_user_id=...)` 拿到 `token`
    2. `await redis_client.delete(f"bind:{token}")` 模拟 TTL 到期
    3. `await api_client.post("/api/v1/auth/redeem-bind-token", json={"bind_token": token, "device_id": "dev1"})`
    4. 断言 `resp.status_code == 400` 且 `resp.json()["detail"] == "bind token invalid or expired"`
    5. 断言 DB 无副作用：`SELECT count(*) FROM auth_tokens WHERE user_id = <child_id>` == 0
    6. 断言 `await redis_client.get(f"bind_result:{token}")` 为 `None`
- **Mutation smoke**：把 `bind.py` 的 `peek_bind_token` 里 `if raw is None: return None` 改成 `if raw is None: return (uuid.uuid4(), uuid.uuid4())`；测试应 fail。

### A2 · 下线→重绑闭环（child_profile 复用）

- 文件：`test_child_bind.py`
- 用例：`test_rebind_after_revoke_reuses_child_profile_and_revokes_old_token`
- 步骤：
    1. 登录 parent → 得 parent token
    2. `POST /api/v1/children` 创建 child → 得 `child_user_id`；`SELECT count(*) FROM child_profiles WHERE child_user_id = ?` == 1
    3. 第一轮：`POST /api/v1/children/{child_user_id}/bind-token` 得 `token_A` → `POST /api/v1/auth/redeem-bind-token` 得 `child_token_1`
    4. `POST /api/v1/children/{child_user_id}/revoke-tokens` 下线 child
    5. 断言 `await resolve_token(db_session, redis_client, child_token_1) is None`
    6. 第二轮：`POST /api/v1/children/{child_user_id}/bind-token` 得 `token_B` → redeem 得 `child_token_2`
    7. 断言 `SELECT count(*) FROM child_profiles WHERE child_user_id = ?` **仍 == 1**（复用，不新建）
    8. 断言 `SELECT count(*) FROM auth_tokens WHERE user_id = ?` == 2，其中 `revoked_at IS NULL` 的有 1 行（是 `child_token_2`）
    9. 断言 `await redis_client.get(f"bind_result:{token_A}")` 与 `f"bind_result:{token_B}"` 两条都存在，解 JSON 后 `child_user_id` 都等于该 child
- **Mutation smoke**：把 `api/auth.py` 的 `redeem_bind_token_endpoint` 里 `await revoke_all_active_tokens(db, child.id)` 一行注释；测试应 fail（`child_token_1` 仍可 resolve）。

### A3 · bind status 过期未扫 → 404

- 文件：`test_child_bind.py`
- 用例：`test_bind_status_404_when_expired_and_never_scanned`
- 步骤：
    1. `issue_bind_token` 得 `token`（不扫码）
    2. `await redis_client.delete(f"bind:{token}")` 模拟 bind TTL 到期
    3. `await api_client.get(f"/api/v1/bind-tokens/{token}/status")`
    4. 断言 `resp.status_code == 404`
- **Mutation smoke**：把 `api/children.py::get_bind_token_status` 最后的 `raise HTTPException(404, ...)` 改成 `return BindTokenStatusOut(status="pending")`；测试应 fail。

### A4 · bind status 过期已扫 → 仍 bound

- 文件：`test_child_bind.py`
- 用例：`test_bind_status_bound_when_bind_key_expired_but_result_alive`
- 步骤：
    1. 完整走完 issue + redeem 流程（保证 `bind_result:<token>` 已写入）
    2. `await redis_client.delete(f"bind:{token}")` 模拟 bind_token 到期；验证 `await redis_client.get(f"bind_result:{token}") is not None`
    3. `await api_client.get(f"/api/v1/bind-tokens/{token}/status")`
    4. 断言 `resp.status_code == 200`；`body["status"] == "bound"`；`body["child_user_id"] == str(child_user_id)`；`body["bound_at"]` 可 parse 为 datetime
- **Mutation smoke**：把 `get_bind_token_status` 中读 `bind_result` 的分支和读 `bind:` exists 的分支顺序对调；测试应 fail（回 404）。

### A5 · status 端点零 DB 依赖（签名契约 + 运行时双重验证）

- 文件：`test_child_bind.py`
- 用例：
    - `test_bind_status_endpoint_signature_has_no_async_session_param`
    - `test_bind_status_endpoint_does_not_invoke_get_db`
- 第一条（签名契约）：
    1. `import inspect; from app.api.children import get_bind_token_status`
    2. `params = inspect.signature(get_bind_token_status).parameters`
    3. `from sqlalchemy.ext.asyncio import AsyncSession`
    4. 断言 `AsyncSession` 不出现在任何 `params[*].annotation` 的类型中（用 `typing.get_type_hints` 展开 `Annotated` 后遍历）
- 第二条（运行时）：
    1. `from app.db import get_db` 拿到 dep 键
    2. 定义一个 `_raise_if_called` 动态报错的 override：`async def _raise_if_called(): raise RuntimeError("get_db called but status should not need DB")`
    3. 用 fixture `app: FastAPI` 的 `app.dependency_overrides[get_db] = _raise_if_called` 覆盖（注意不要覆写现有的 `get_redis` 覆盖）
    4. 先跑 issue+redeem 准备 bind_result（这部分用原始 `get_db` 覆盖，可以在那之后再换成 raise 版）
    5. 调 `await api_client.get(f"/api/v1/bind-tokens/{token}/status")`
    6. 断言 `resp.status_code == 200`（如果端点内部触发了 get_db，会抛 500）
- **Mutation smoke**：在 `get_bind_token_status` 签名里追加 `db: Annotated[AsyncSession, Depends(get_db)] = ...`；两条测试都应 fail。

### A6 · verify_password 非法 hash 异常上抛（锁定 fail-fast 契约）

- 文件：`test_password.py`
- 决策依据：`password.py` 注释明确声称 "其它 argon2 异常属于数据损坏，向上抛"。A6 锁定的是 **不静默吞异常** 的 fail-fast 契约（而非返回 False）。
- 用例：
    - `test_verify_password_raises_invalid_hash_on_non_argon2_string`
    - `test_verify_password_raises_invalid_hash_on_truncated_hash`
    - `test_verify_password_raises_invalid_hash_on_empty_string`
- 步骤：
    1. `from argon2.exceptions import InvalidHash` —— 若该符号不存在，回退到 `from argon2.exceptions import InvalidHashError as InvalidHash`（二选一，按 argon2-cffi 实际导出名适配，在用例顶部放一个 `try/except ImportError` 适配器）
    2. `with pytest.raises(InvalidHash): verify_password("not-a-hash", "any")`
    3. `with pytest.raises(InvalidHash): verify_password(hash_password("x")[:30], "any")`（截断）
    4. `with pytest.raises(InvalidHash): verify_password("", "any")`
    5. **不要** 断言 `verify_password(...) is False`；那会违背设计。
- **Mutation smoke**：把 `password.py` 的 `except VerifyMismatchError:` 改成 `except Exception:`；三条测试都应 fail（原本抛的变成返 False）。

### A7 · login rate-limit 窗口到期后计数重置

- 文件：`test_login_api.py`
- 用例：`test_login_rate_limit_counter_resets_after_window_expiry`
- 步骤：
    1. 用现有 `parent_with_password` 类 fixture（若文件内有，没有则沿用其内部方式自种），phone 设为 `"abcd"`
    2. 错误密码连打 5 次 `POST /api/v1/auth/login`；第 1–5 次断言 401
    3. 第 6 次断言 429 且 `detail == "too many attempts; try again later"`
    4. `await redis_client.delete("login_fail:phone:abcd")` 与 `await redis_client.delete("login_fail:ip:<client_ip>")` — `<client_ip>` 用 httpx ASGI 的客户端 IP（通常 `"testclient"` 或 `None`；实际执行时先查一下 `redis_client.keys("login_fail:ip:*")` 拿真实键名）
    5. 再打一次（仍错密码）断言 `status_code == 401`（**不是** 429）
    6. 断言 `await redis_client.get("login_fail:phone:abcd") == "1"`（`decode_responses=True`）
- **Mutation smoke**：把 `api/auth.py::_check_login_limit` 里 `phone_count >= LOGIN_PHONE_LIMIT` 改成 `phone_count > LOGIN_PHONE_LIMIT`；测试应 fail（第 6 次不再包 429）。

## 🅱️ Phase B · P1 账号体系重写

### B1 · 重写 same-day skip 验证

- 第一步先 locate：`cd backend && grep -rn "same_day_skips" tests/` 找到实际文件（预期在 `test_auth_deps.py` 或 `test_tokens.py`）。若暂未定位到该名称，找所有断言 `updated_at_after - updated_at_before == 0` 或类似 delta 模式的用例。
- 删除：原用例整体删掉
- 新增（在同文件）：`test_get_current_account_same_day_skips_roll_token_call`
- 方案锁定：**方案二（monkeypatch counter）**。理由：方案一的 `AsyncSession.execute.call_count` 会被 `resolve_token` miss 时的 SELECT 污染，混淆语义。
- 步骤：
    1. `from app.auth import deps as auth_deps`
    2. 在 test 内部定义：
    
    ```python
    calls = []
    original = auth_deps.roll_token_expiry
    async def _counting(*args, **kwargs):
    	calls.append((args, kwargs))
    	return await original(*args, **kwargs)
    monkeypatch.setattr("app.auth.deps.roll_token_expiry", _counting)
    ```
    
    1. issue 一个 parent token + commit_with_redis
    2. 同一 device_id 连打两次认证端点（任选 `GET /api/v1/me` 或别的需 `get_current_account` 的路由），header 带 `Authorization: Bearer <token>` + `X-Device-Id: <dev>`
    3. 断言 `len(calls) == 1`
    4. 附加断言 Redis 里 payload.last_rolled_date == 今天（防止未更新的软失败）
- **Mutation smoke**：把 `tokens.py::needs_roll` 的 `payload.last_rolled_date != _today_cst()` 改成常量 `True`；测试应 fail（`len(calls) == 2`）。

### B2 · Redis payload 跨 call 可见性（原题重定义）

- 背景：原 B2 想验证 "跨 DB session 可见" 在 conftest savepoint 架构下无法做到。重定义为验证 Redis 带来的真正跨请求可见性。
- 文件：`test_tokens.py`
- 用例：`test_roll_token_plus_commit_updates_redis_payload`
- 步骤：
    1. issue token + commit_with_redis
    2. `old_raw = await redis_client.get(f"auth:{th}")`；`old = TokenPayload.model_validate_json(old_raw)`
    3. monkeypatch `app.auth.tokens._today_cst` 返回 `"2026-04-20"`（倒到昨天），保证 needs_roll 底层逻辑有意义；另外 fresh resolve 一次拿到 payload
    4. 停 monkeypatch，调 `roll_token_expiry(db_session, token_hash_hex=th, payload=payload)`
    5. `commit_with_redis(db_session, redis_client)`
    6. `new_raw = await redis_client.get(f"auth:{th}")`；`new = TokenPayload.model_validate_json(new_raw)`
    7. 断言 `new.expires_at > old.expires_at`；`new.last_rolled_date == _today_cst()`（真今日）；`old_raw != new_raw`
- **Mutation smoke**：把 `roll_token_expiry` 末尾的 `stage_redis_op(...)` 注释；测试应 fail（Redis 里仍是旧 payload）。
- 偏差记录：在 [M4 执行偏差记录](https://www.notion.so/M4-9341dc6b128f448ea1aeaa733de9ae81?pvs=21) 中添加一条 "B2 原定义被下放到 M5 前的 integration 层"。

### B3 · 重写 `test_roll_token_stage_redis_op_not_committed`（去 rollback 遮蔽 + 补 Redis 未写断言）

- 文件：`test_tokens.py::TestRollTokenExpiry`
- 用例：用同名替换原版 `test_roll_token_stage_redis_op_not_committed`
- 步骤：
    1. issue + commit_with_redis → Redis 有初始 payload
    2. `old_raw = await redis_client.get(f"auth:{th}")`
    3. `payload = await resolve_token(db_session, redis_client, token)` （cache 命中）
    4. 调 `await roll_token_expiry(db_session, token_hash_hex=th, payload=payload)`
    5. 断言：`pending = db_session.info.get("pending_redis_ops", [])`；`any(op.kind == "setex" and op.key == f"auth:{th}" for op in pending)`
    6. **关键新增断言**：`now_raw = await redis_client.get(f"auth:{th}")`；`assert now_raw == old_raw`（Redis 没被写过）
    7. 清理：`discard_pending_redis_ops(db_session)`（避免触 teardown 护栏）
    8. **不要** `db_session.rollback()`；rollback 不是验证目标路径
- **Mutation smoke**：把 `tokens.py::roll_token_expiry` 的 `stage_redis_op(...)` 换成直接 `await redis.setex(...)`（需注入 redis 参数，不好改就改写为 `stage_redis_op` 后套一层 立刻 EXEC）；测试应 fail。

### B4 · 补 roll_token 后 last_rolled_date 底层断言（JSON field，非 hash field）

- 文件：`test_tokens.py::TestRollTokenExpiry`
- 用例：`test_roll_token_updates_last_rolled_date_in_redis_payload`
- 步骤：
    1. issue + commit → Redis 里 `payload.last_rolled_date == _today_cst()`（已知行为）
    2. monkeypatch `app.auth.tokens._today_cst` 返回 `"2026-04-20"`（昨天）
    3. fresh resolve 拿到 payload（last_rolled_date 从 cache 里看起来仍是原始今天；注意：resolve 走 cache hit 路径不修改 last_rolled_date）。为让逻辑进 `needs_roll=True` 分支，手动构造 payload：`payload = payload.model_copy(update={"last_rolled_date": "2026-04-20"})`
    4. `roll_token_expiry(db_session, token_hash_hex=th, payload=payload)` + `commit_with_redis`
    5. 停 monkeypatch
    6. `new_raw = await redis_client.get(f"auth:{th}")`；`new = TokenPayload.model_validate_json(new_raw)`
    7. 断言 `new.last_rolled_date == _today_cst()`（真今日）
    8. 断言 `needs_roll(new) is False`
- **注意**：原计划写的 `redis_client.hget(...)` **是错的** —— Redis 里存的是 JSON 字符串，不是 hash。本版本已校正。
- **Mutation smoke**：把 `roll_token_expiry` 里 `"last_rolled_date": _today_cst()` 改成 `"last_rolled_date": None`；测试应 fail。

## 🅾️ Phase C · P2 账号体系清理

### C1 · create_child DB 落地断言

- 文件：`test_child_bind.py`（或实际存放 create_child 测试的文件，先 grep `POST /api/v1/children` 测例定位）
- 在原有 response 断言后追加：
    - `SELECT count(*) FROM users WHERE id = ? AND role = 'child' AND family_id = ?` == 1
    - `SELECT count(*) FROM child_profiles WHERE child_user_id = ? AND created_by = ?` == 1
    - `SELECT birth_date, gender FROM child_profiles WHERE child_user_id = ?` 的值与请求 payload 匹配

### C2 · CLI 脚本 DB 落地

- 文件：`test_scripts.py`
- 对每条 stdout 断言后补一段 `async with async_session(engine) as s: rows = await s.execute(...)` 断言 `families` / `users` / `family_members` / `child_profiles` 的行数与该命令的预期一致
- 如果现有测试走的是主库（非 `littlebox_test`），先改到 `_test_url()` 来避免污染开发库。

### C3 · 响应屏蔽断言扩展

- 文件：`test_login_api.py`
- 把现有 "response 不含 `password_hash` / `hashed_password`" 的断言抽成辅助函数 `_assert_no_secret_fields(body)`，扩展断言 key 列表至少包含：`password_hash` / `hashed_password` / `secret` / `token_hash`
- 之后在这些端点的成功响应上断言：
    - `POST /api/v1/auth/login`（已有，换上辅助函）
    - `POST /api/v1/auth/redeem-bind-token`（新增）
    - `POST /api/v1/children`（新增）
    - `GET  /api/v1/bind-tokens/{token}/status` bound 响应（新增）

### C4 · fixture 收敛 `seeded_parent`

- 文件：`backend/tests/conftest.py`
- 取消注释文尾 `seeded_parent` fixture 块（已知签名为 `(user: User, plaintext_password: str)`）
- 并行改造调用方：
    - `test_tokens.py::parent_user` / `child_user`：不迁移至 `seeded_parent`（因为不需密码），保留
    - `test_login_api.py` 自造的 `parent_with_password` / 类似→ **迁移到 `seeded_parent`**
    - `test_auth_deps.py` / `test_child_bind.py` / `test_e2e_auth.py` 的同类子 fixture→ 能用 `seeded_parent` 就用；需 plain password 的都用 `seeded_parent`
- 正则检查：收敛后 `grep -rn "hash_password(" backend/tests/` 只应在 `conftest.py` 和 `test_password.py` 中出现

### C5 · 已撤销

- 原 "E2E teardown TRUNCATE" 条目 —— conftest outer rollback 已全域兼顾，重复劳动，**不做**。记录在偏差页。

## 🅳️ Phase D · P3 非 M4 模块补强（chat / graph / SSE）

已从源码核对，所有用例以下述事实为准。保留的 `test_conftest_smoke.py`（5 条）和 `test_llm_smoke.py`（1 条 @live）不动——前者是 function-scope + NullPool 契约守门测，后者三重守护已到位。

### D1 · 修 `test_astream_yields_delta_chunks` 重复断言 + 补全 finish chunk 验证

- 文件：`test_dashscope_chat.py`
- 性质：**原地修改** + 重命名为 `test_astream_yields_content_and_finish_chunks`
- 具体动作：
    1. 删除重复行（原第 84–85 行）：`assert result_chunks[0].text == "你"` 和 `assert result_chunks[1].text == "好"` 的第二次出现
    2. 追加断言：
    
    ```python
    assert result_chunks[3].text == ""
    assert result_chunks[3].message.response_metadata == {"finish_reason": "stop"}
    assert result_chunks[3].message.usage_metadata == {
    	"input_tokens": 10,
    	"output_tokens": 2,
    	"total_tokens": 12,
    }
    ```
    
    1. 注释行中 `# 4 chunks total (empty content chunk + finish chunk)` 保留
- **Mutation smoke**：删除 `dashscope_chat.py::_astream` 里 `finish_reason in ("stop", ...)` 分支的整个 `yield ChatGenerationChunk(...)` 语句；测试应 fail（长度变成 3 且第 4 条断言触索引越界）。

### D2 · 新增 `test_astream_empty_content_still_yields_empty_delta`

- 文件：`test_dashscope_chat.py`
- 目的：锁定 `_astream` 每条 response 无条件 yield（即使 delta 为空）的契约。
- 步骤：
    1. 构造 2 个 upstream chunk：第一个 `content=[]`（思考阶段）、第二个 `content=[{"text": "你好"}], finish_reason="stop"`
    2. 迭代 `_astream` 结果
    3. 断言 `len(result_chunks) == 3`：空 delta / 实 delta / finish chunk
    4. 断言 `result_chunks[0].text == ""`；`result_chunks[1].text == "你好"`；`result_chunks[2].message.response_metadata["finish_reason"] == "stop"`
- **Mutation smoke**：在 `_astream` 的 `yield ChatGenerationChunk(...)` 前增加 `if not delta: continue`；测试应 fail（`len == 2`）。

### D3 · 新增 `test_astream_multi_content_parts_concatenated`

- 文件：`test_dashscope_chat.py`
- 目的：锁定多 content item 通过 `"".join(part.get("text", "") for part in raw if isinstance(part, dict))` 拼接，且忽略非 dict / 无 text key。
- 步骤：
    1. 构造 1 个 upstream chunk，`content=[{"text": "hello"}, {"text": " world"}, {"other": "ignored"}, "string-should-skip"], finish_reason="stop"`
    2. 迭代 `_astream`
    3. 断言 `result_chunks[0].text == "hello world"`（第三四个被跳过）
- **Mutation smoke**：把 `delta = "".join(part.get("text", "") for part in raw if isinstance(part, dict))` 改成 `delta = raw[0].get("text", "") if isinstance(raw[0], dict) else ""`；测试应 fail（只得 `"hello"`）。

### D4 · 新增 `test_astream_non_whitelist_finish_reason_does_not_emit_finish_chunk`

- 文件：`test_dashscope_chat.py`
- 目的：锁定白名单 `("stop", "length", "tool_calls", "content_filter")`。DashScope 曾发过 `"nullnullstop"` 这种非终止态的 finish_reason（M3 踩坑记录），不能被当终止透传。
- 步骤：
    1. 构造 chunk `content=[{"text": "你"}], finish_reason="nullnullstop"`
    2. 迭代 `_astream`
    3. 断言 `len(result_chunks) == 1`（只有一个 content yield，无 finish chunk 追加）
    4. 断言 `result_chunks[0].message.response_metadata == {}`（或不含 `finish_reason`）
- **Mutation smoke**：把 `if choice.finish_reason in ("stop", ...):` 改成 `if choice.finish_reason:`；测试应 fail（会出现 finish chunk）。

### D5 · 新增 `test_astream_passes_enable_thinking_to_sdk`

- 文件：`test_dashscope_chat.py`
- 目的：锁定 `enable_thinking` 实际传递给 DashScope SDK，避免默认值被硬编码。
- 步骤：
    1. `llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test", enable_thinking=True)`
    2. 构造一个最小的 mock_stream（单 stop chunk）
    3. `with patch.object(AioMultiModalConversation, "call") as mock_call: mock_call.return_value = mock_stream()`
    4. `async for _ in llm._astream([MagicMock(type="human", content="hi")]): pass`
    5. `mock_call.assert_called_once()`；`kwargs = mock_call.call_args.kwargs`；`assert kwargs["enable_thinking"] is True`；`assert kwargs["stream"] is True`；`assert kwargs["incremental_output"] is True`；`assert kwargs["result_format"] == "message"`
- **Mutation smoke**：把 `enable_thinking=self.enable_thinking` 改成 `enable_thinking=False`；测试应 fail。

### D6 · 新增 `test_graph_events_ordered_start_then_streams_then_end`

- 文件：`test_graph_stream.py`
- 目的：锁定事件顺序 `on_chain_start → on_chat_model_stream* → on_chain_end`。
- 步骤：
    1. 复用现有 GenericFakeChatModel monkeypatch 方式（注意 `app.chat.graph.get_chat_llm` 使用点）
    2. `events = [e async for e in graph.astream_events({...}, version="v2")]`
    3. 提取目标事件序列：`names = [e["event"] for e in events if e["event"] in ("on_chain_start", "on_chat_model_stream", "on_chain_end")]`
    4. 断言 `names[0] == "on_chain_start"`；`names[-1] == "on_chain_end"`
    5. 断言中间至少有一个 `on_chat_model_stream`；所有 `on_chat_model_stream` 的 index 均 > 0 且 < len(names) - 1
    6. 断言存在 `on_chain_start` 事件，`e["name"]` 包含 `"call_main_llm"`（节点名）
- **Mutation smoke**：把 `graph.py` 中 `builder.add_edge("call_main_llm", END)` 删掉，改成 `builder.set_finish_point("call_main_llm")` 之外的不终止配置；测试应 fail。（替代 mutation：把 `llm.ainvoke` 前的 `messages` 参数改成空，on_chat_model_stream 数量为 0，触发“至少有一个”断言 fail）

### D7 · 新增 `test_graph_node_exception_surfaces_as_on_chain_error_and_reraises`

- 文件：`test_graph_stream.py`
- 目的：锁定节点内异常会（1）透出 `on_chain_error` 事件，（2）最终从 `astream_events` 重抛。
- 步骤：
    1. `class _ExplodingLLM:` 构造一个 `ainvoke` 抛 `RuntimeError("node boom")` 的最小 LLM（继承或负当 `BaseChatModel` 都行；同时开放 `disable_streaming=False`）。或更简单：用 `unittest.mock.AsyncMock(side_effect=RuntimeError("node boom"))` 作为 `llm.ainvoke`
    2. `monkeypatch.setattr("app.chat.graph.get_chat_llm", lambda: _llm)`
    3. 运行：
    
    ```python
    events = []
    with pytest.raises(RuntimeError, match="node boom"):
    	async for e in graph.astream_events({"messages": [HumanMessage("hi")]}, version="v2"):
    		events.append(e)
    ```
    
    1. 断言 `any(e["event"] == "on_chain_error" for e in events)`
- **Mutation smoke**：在 `graph.py::call_main_llm` 外包 `try/except Exception: return {"messages": []}`；测试应 fail（异常被吞）。

### D8 · 新增 `test_graph_messages_reducer_appends_not_overwrites`

- 文件：`test_graph_stream.py`
- 目的：锁定 `ChatState.messages` 的 `add_messages` reducer（追加语义）。保护 M6 之前不被无意改回覆盖语义。
- 步骤：
    1. monkeypatch `app.chat.graph.get_chat_llm` 返回 `GenericFakeChatModel(messages=iter([AIMessage(content="你好")]))`
    2. `result = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})`
    3. 断言 `len(result["messages"]) == 2`
    4. 断言 `result["messages"][0].type == "human"` 且 content=="hi"
    5. 断言 `result["messages"][1].type == "ai"` 且 content=="你好"
- **Mutation smoke**：把 `ChatState.messages: Annotated[list[BaseMessage], add_messages]` 改成 `messages: list[BaseMessage]`（去掉 reducer）；测试应 fail（len==1，输入被覆盖）。

### D9 · 重写 `test_sse_disconnect_path`（锁定 docstring 承诺）

- 文件：`test_sse_endpoint.py`
- 性质：原地重写
- 问题：现有 disconnect 用例只断言 `start` 存在，docstring 承诺的 “无 `CancelledError` 堆栈 / 不向已断客户端发 error” 都没有验证。
- 步骤：
    1. 增加签名：`async def test_sse_disconnect_path(monkeypatch, caplog):`
    2. 开头：`import logging; caplog.set_level(logging.ERROR, logger="uvicorn.error")` 并也 `caplog.set_level(logging.ERROR)` 默认 root
    3. 复用现有 slow_stream + GenericFakeChatModel patch
    4. 实现客户端：收到 `"delta"` 后 `break`（原有代码保留）
    5. 在原有断言后追加：
        - `assert any(e["type"] == "start" for e in events)`（已有）
        - **新：** `assert not any(e["type"] == "error" for e in events)`（断开时不能发 error）
        - **新：** `assert not any(e["type"] == "end" for e in events)`（取消后不应走到 end）
        - **新：** 等 ≤ 200ms 让 server 代理 task 清理：`await asyncio.sleep(0.2)`
        - **新：** `error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]`；`assert not any("CancelledError" in (r.exc_text or "") or "CancelledError" in r.getMessage() for r in error_records)`；`assert not any("BrokenResourceError" in (r.exc_text or "") or "BrokenResourceError" in r.getMessage() for r in error_records)`
- **Mutation smoke**：把 `sse.py` 里 `except (asyncio.CancelledError, ClientDisconnect, anyio.BrokenResourceError): raise` 改成 `except (asyncio.CancelledError, ClientDisconnect, anyio.BrokenResourceError): pass`（吞掉取消，会继续走到末尾 `yield end`，导致 BrokenResourceError 写失败→ caplog 有 ERROR 日志）；测试应 fail。

### D10 · 新增 `test_sse_happy_path_event_schema_locked`

- 文件：`test_sse_endpoint.py`
- 目的：锁定 happy path 事件的字段 schema，防止 SSE wire 格式默默漂移。
- 步骤：
    1. 复用原 `test_sse_happy_path` 的 patch 布置
    2. 拿到 `events` 后：
        - `start = events[0]`；`assert set(start.keys()) == {"type", "session_id"}`；`assert start["type"] == "start"`；`import uuid; uuid.UUID(start["session_id"])` 不抛
        - `deltas = [e for e in events if e["type"] == "delta"]`；`assert len(deltas) >= 1`；`for d in deltas: assert set(d.keys()) == {"type", "content"}; assert isinstance(d["content"], str) and d["content"]`
        - `end = events[-1]`；`assert set(end.keys()) == {"type", "finish_reason"}`；`assert end["finish_reason"] == "stop"`
- **Mutation smoke**：把 `sse.py` 的 `yield _sse_pack("end", finish_reason="stop")` 改成 `yield _sse_pack("end")`；测试应 fail（end 字段集不符）。

### D11 · 新增 `test_sse_error_event_schema_and_no_end_after_error`

- 文件：`test_sse_endpoint.py`
- 目的：锁定 error 路径的 schema 与“error 后不再发 end”的契约（源码中 `return` 实现）。
- 步骤：
    1. 复用原 `test_sse_error_path` 的 `error_getter` patch
    2. 运行后拿到 events
    3. `err = next(e for e in events if e["type"] == "error")`；`assert set(err.keys()) == {"type", "message", "code"}`；`assert err["code"] == "RuntimeError"`；`assert "upstream error" in err["message"]`
    4. `assert not any(e["type"] == "end" for e in events)`（error 后有 `return`，不走 end）
- **Mutation smoke**：把 `sse.py` 的通用 except 里 `yield _sse_pack("error", ...); return` 里的 `return` 删掉；测试应 fail（会出现 end）。

### D12 · 新增 `test_dev_chat_stream_rejects_invalid_message_length`

- 文件：`test_sse_endpoint.py`（与现有 SSE 测试同文件，复用 ASGITransport）
- 目的：锁定 `DevChatRequest` 的 `min_length=1, max_length=2000` 约束。
- 步骤：
    1. `transport = ASGITransport(app=app); async with AsyncClient(transport=transport, base_url="http://test") as client:`
    2. `resp = await client.post("/api/dev/chat/stream", json={"message": ""})`；断言 `resp.status_code == 422`
    3. `resp = await client.post("/api/dev/chat/stream", json={"message": "x" * 2001})`；断言 `resp.status_code == 422`
    4. 注意不用 patch LLM，因为为 422 在校验层就拦截了
- **Mutation smoke**：把 `dev_chat.py` 里 `message: str = Field(..., min_length=1, max_length=2000)` 改成 `message: str`；两条断言都应 fail。

## 📋 执行顺序与 commit 表

| # | 阶段 | 文件 | commit 消息 |
| --- | --- | --- | --- |
| 1 | A1 | `test_child_bind.py` | `test(m4-patch): bind token expired redeem 400` |
| 2 | A2 | `test_child_bind.py` | `test(m4-patch): rebind after revoke reuses child_profile` |
| 3 | A3 | `test_child_bind.py` | `test(m4-patch): bind status 404 when expired unscanned` |
| 4 | A4 | `test_child_bind.py` | `test(m4-patch): bind status bound when bind expired but result alive` |
| 5 | A5 | `test_child_bind.py` | `test(m4-patch): bind status endpoint no db dependency` |
| 6 | A6 | `test_password.py` | `test(m4-patch): verify_password raises on invalid hash` |
| 7 | A7 | `test_login_api.py` | `test(m4-patch): login rate limit counter resets after window` |
| 8 | B1 | 待 locate | `test(m4-patch): rewrite same-day skip via monkeypatch counter` |
| 9 | B2 | `test_tokens.py` | `test(m4-patch): roll_token plus commit updates redis payload` |
| 10 | B3 | `test_tokens.py` | `test(m4-patch): roll_token not committed leaves redis untouched` |
| 11 | B4 | `test_tokens.py` | `test(m4-patch): roll_token updates last_rolled_date in redis payload` |
| 12 | C1–C4 | 多文件 | `test(m4-patch): phase-c cleanup (db landing, cli, response masking, fixture consolidation)` |
| 13 | D1–D5 | `test_dashscope_chat.py` | `test(chat): dashscope hardening (finish chunk, empty delta, multi-part, whitelist, enable_thinking)` |
| 14 | D6–D8 | `test_graph_stream.py` | `test(chat): graph hardening (event order, node error, reducer)` |
| 15 | D9–D12 | `test_sse_endpoint.py` | `test(chat): sse hardening (disconnect, schema, error, req validation)` |

## ✅ 验收标准

- [ ]  Phase A 全绿：10 条新增用例全部 pass（A1/A2/A3/A4/A7 各 1 + A5×2 + A6×3）
- [ ]  Phase B 全绿：4 条重写/新增全部 pass；原 B1 delta 断言用例不再存在；原 B3 `session.rollback()` 遮蔽路径已移除
- [ ]  Phase C 全绿：C1–C4 的 commit 均绿；`grep -rn "hash_password(" backend/tests/` 仅在 `conftest.py` 和 `test_password.py`
- [ ]  Phase D 全绿：
    - [ ]  `test_dashscope_chat.py` 总数从 5 → 9（D1 原地改 + D2/D3/D4/D5 四条新增）
    - [ ]  `test_graph_stream.py` 总数从 3 → 6（D6/D7/D8 新增）
    - [ ]  `test_sse_endpoint.py` 总数从 3 → 6（D9 原地改 + D10/D11/D12 三条新增）
- [ ]  全量回归：`cd backend && pytest` 通过数 ≥ 160（原 138 + A 新增 10 + B 新增 2（B2/B4；B1/B3 为重写同名，不变数）+ D 新增 10（D2–D5 四 + D6–D8 三 + D10–D12 三）+ C 不变数 = 160；以实际运行为准）
- [ ]  mutation smoke 记录：PR 描述里列出 A1–A7 / B1–B4 / D1–D12 每条 mutation 做过（打勾）
- [ ]  无 skip（`@pytest.mark.live` 除外）；无 `xfail`；无 `sleep(>0.2)`（D9 里的 0.2s 等待允许）
- [ ]  不引入新的 session-scope fixture；不新增包依赖
- [ ]  不改动 `backend/app/**` 生产代码（`git diff main -- backend/app` 为空）

## ⚠️ 风险与降级

- **argon2 异常类名**（A6）：argon2-cffi 历史版本分别导出 `InvalidHash` 和 `InvalidHashError`。用两段式 `try/except ImportError` 适配，不要强绑单一名称。
- **客户端 IP**（A7）：httpx ASGI transport 的 `request.client` 可能为 `None`，此时源码落到 `"unknown"` 作为 IP。先 `redis_client.keys("login_fail:ip:*")` 拿真实 key 名再删，不要硬写。
- **A5 get_db raise override 作用面**：只在 status 调用周期内面 raise；issue+redeem 准备数据时先用正常 override，准备完再换成 raise 版。记得测完还原。
- **B1 monkeypatch 路径**：必须 patch `app.auth.deps.roll_token_expiry`（使用点），**不是** `app.auth.tokens.roll_token_expiry`（定义点）。Python import 绑定坑。
- **B4 monkeypatch 顺序**：先 patch `_today_cst` 再构造 payload，然后 patch 恢复后再 commit + 读 Redis。交错顺序会导致断言混乱。
- **D6/D7/D8 import 绑定**：所有 `monkeypatch.setattr` 目标必须是 **`app.chat.graph.get_chat_llm`**（使用点），不是 `app.chat.llm.get_chat_llm`（定义点）。与 B1 同坑。
- **D7 抛异常的 LLM**：要避免 `GenericFakeChatModel` 的 messages 迭代器在 `ainvoke` 时自己抛 StopIteration；用 `AsyncMock(side_effect=RuntimeError(...))` 替换 llm.ainvoke 最稳。
- **D9 caplog 与 ASGI 任务端清理**：`async with client.stream(...)` 退出代表 HTTP 层关连，但 Starlette stream task 可能稍晚写到 CancelledError。加 `await asyncio.sleep(0.2)` 让 task teardown 完成再查 caplog。
- **时间预算**：Phase A 0.5–1 day · Phase B 0.5 day · Phase C 0.5 day · Phase D 1 day。合计 ≤ 3 工日。

## 📌 跟踪

- 合并后在 [](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21) 新增：
    - "跨 connection DB 可见性 integration 测"（需 conftest 改架构，归属下一个 infra patch）
- 若补测过程中证伪出生产代码缺陷，停手，开独立页面并链接回本 patch。
- 特别提醒：`app/api/dev_chat.py` 文件头写明 "M7 聊天界面正式版上线后整文件删除"。D10–D12 锁定的是该 dev 路由的 schema，删文件时同步删除对应测试用例。