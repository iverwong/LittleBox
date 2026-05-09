# M6-patch · 测试隔离纪律加固

<aside>
📍

**触发**：2026-04-30 排查 dev_chat 链路时打开 `littlebox` 开发库，发现 `admin_note='e2e-test parent'` + 随机 4 字母 phone 的 user 一批长期累积，伴随对应 family / family_members 残留。

**结论**：`backend/tests/test_e2e_auth.py` 完全旁路了 `conftest.py` 的测试隔离体系；阶段 1 核查另发现 `test_scripts.py` 同类破口。本 patch 全清残留、重构 CLI 业务/IO 分离、重写两个测试文件、加双层运行时防御。

</aside>

## 1. 背景与定位

`backend/tests/conftest.py` 已设计好完整隔离：独立 `littlebox_test` 库 + session 头 DROP/CREATE + alembic upgrade + function 级 savepoint rollback + fakeredis 进程内模拟 + FastAPI `dependency_overrides`。其余测试老实走 `api_client` / `db_session` / `redis_client` fixture，**破口为 `test_e2e_auth.py` + `test_scripts.py` 两个文件**。

破口证据（阶段 1 核查实测）：

- `test_e2e_auth.py:45-68` — `subprocess.Popen([sys.executable, "-m", "app.scripts.create_parent" / "...reset_parent_password", ...])`，通过 `os.environ` 继承 `LB_DATABASE_URL` 调真实 CLI，写真库 `littlebox`
- `test_e2e_auth.py:91-92` — `httpx.Client(base_url="http://localhost:8000")` 直连容器内真实 HTTP server
- `test_e2e_auth.py:73-78` — `redis_lib.Redis(host="redis", port=6379).flushdb()` 真连 Redis 容器
- `test_scripts.py:117-260` — 3 处 `subprocess.run([sys.executable, "-c", check_script])` 内联 `create_async_engine(_TEST_DB_URL)`，绕开 conftest engine fixture（虽只访问 `littlebox_test`，但单 session 内真落盘干扰其他测试）
- 两个文件均零 teardown：`test_e2e_auth.py` 仅 flush Redis 不删 DB 行；phone 是 random 4 字母，每跑一次累计

## 2. 影响范围

- **开发库 `littlebox` 脏数据累积**：`test_e2e_auth.py` 跑一次累积 1 parent + 1 family + 2 family_members + N 个 Redis bind key
- **Redis 牵连**：`flushdb()` 清掉同实例其他业务 key（dev hub / 调试 token / 限流计数器）
- **`littlebox_test` 单 session 内污染**：`test_scripts.py` 真落盘到 `littlebox_test`，干扰同 session 后续测试
- **测试不可重入**：phone 随机碰撞会让 CLI 子进程在 unique 约束失败时整套挂掉
- **CI / pytest-xdist 并发风险指数级放大**

## 3. 决策（D1–D11，已与 Iver 对齐）

- **D1 范围**：全部一次性修 — 两破口 + CLI 拆分 + T4 防御 + 文档
- **D2 CLI 重构**：**方案 C** — `_create_parent(db, redis, *, note)` 强制注入；新增 `_main()` 仅做 argparse + `cli_runtime`；CLI 与测试共用业务函数，wrapper 极薄无可漏测
- **D3 `test_scripts.py`**：重写为业务函数测试 + 1 个 CLI 入口 smoke（无 subprocess）
- **D4 `test_e2e_auth.py`**：完全重写为 ASGI in-process + fixture 注入，保留文件名以承载 CLI→HTTP→CLI 串联流
- **D5 数据清理**：执行 agent 跑 `docker compose exec` + psql / redis-cli，无需手动
- **D6 SQL 修订**：字段 `admin_note`（非 `note`）；bind 残留改清 Redis `bind:*`；外键级联顺序 `family_members → users → families`
- **D7 T4 (a) 断言**：模块级双断言 — `_test_url()` 必须含 `_test`，且与 `settings.database_url` 关系明确
- **D8 T4 (b) 兜底**：session autouse fixture — startup 记录真库 baseline，sessionfinish 比对，差异即 fail
- **D9 页面策略**：直接更新本页，不另开
- **D10 分支命名**：`fix/m6-patch-test-isolation`
- **D11 Step 拆分**：7 步 — 建分支 → 清理 → CLI 重构 → 重写 test_scripts → 重写 test_e2e_auth → 防御加固 → 文档

## 4. 目标概述

- **目标**：消除两个测试文件对开发库 / 真 Redis / 真 HTTP server 的依赖，让 backend 全测试套件在 conftest 隔离体系内运行；建立双层运行时防御，杜绝同类破口再生
- **不做什么**：
    - 不改 `conftest.py` 现有 fixture 实现（方向正确）
    - 不引入 pytest-xdist 多 worker 隔离（留待 CI 接入时一起做）
    - 不动 frontend e2e（范围限定 backend）
    - 不为「真容器 + 真 server」保真度单独建 `littlebox_e2e` 库（如未来确需再开 patch）
    - 不改 `cli_runtime()` 实现（生产路径不变）
    - 不重构 `chat/` 子目录测试（核查确认无破口）
    - 不做 pre-commit 钩子（对 AI 辅助开发不友好）

## 5. 前置条件

- 本机 docker compose 可正常启动 `backend` / `db` / `redis` 三容器
- `LB_DATABASE_URL` 环境变量指向开发库 `littlebox`，能 `docker compose exec db psql -U postgres -d littlebox` 进 shell
- 改动前 `pytest backend/tests/ -x` 在现有测试集上能跑通（基线）
- `alembic upgrade head` 在 `littlebox_test` 上能成功跑通
- `basedpyright` 当前无报错（基线）

## 6. 执行步骤

### Step 1 · 建分支 + 准备 cleanup 脚本骨架

**任务**

- [ ]  `git checkout main && git pull`
- [ ]  `git checkout -b fix/m6-patch-test-isolation`
- [ ]  创建目录 `backend/scripts/cleanup/` 并新增 `m6_patch_cleanup.sql`（先写 SELECT 块 + DELETE 注释模板）
- [ ]  新增 `backend/scripts/cleanup/m6_patch_cleanup.md` 记录用途、执行命令、安全闸门

**关键代码片段**

`m6_patch_cleanup.sql`（DELETE 部分先注释，Step 2 抽样核实后再放开）：

```sql
-- M6-patch 一次性清理：清除 e2e-test 残留账户及关联
-- 仅在确认开发库 littlebox 受 e2e 污染时执行
-- 参考: M6-patch · 测试隔离纪律加固

-- 1. 计数 + 抽样（必须先执行确认）
SELECT COUNT(*) AS dirty_users FROM users
WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$';

SELECT id, phone, admin_note, role, created_at FROM users
WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$'
ORDER BY created_at LIMIT 5;

-- 2. 抽样核实通过后取消下方注释，事务包裹执行
-- BEGIN;
-- DELETE FROM family_members WHERE user_id IN (
--   SELECT id FROM users
--   WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$'
-- );
-- DELETE FROM users
-- WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$';
-- DELETE FROM families WHERE id NOT IN (
--   SELECT DISTINCT family_id FROM family_members
-- );
-- COMMIT;

-- 3. 清理后验证（应全为 0）
-- SELECT COUNT(*) FROM users
-- WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$';
```

**验证清单**

- ✅ 分支已切到 `fix/m6-patch-test-isolation`
- ✅ `backend/scripts/cleanup/m6_patch_cleanup.sql` / `.md` 已存在
- ⏸ DELETE 部分仍为注释（Step 2 才放开）

**Commit**：`chore: scaffold m6-patch test isolation branch and cleanup template`

### Step 2 · 数据清理执行

**任务**

- [ ]  跑 `m6_patch_cleanup.sql` 头部 SELECT 块，把计数 + 抽样输出贴回对话
- [ ]  抽样核实命中 e2e 特征后，编辑 SQL 放开 DELETE 注释，跑完整事务
- [ ]  SELECT 验证 users 残留 = 0
- [ ]  清理 Redis `bind:*` 残留
- [ ]  把 DELETE 重新注释回去，保留脚本作仓库纪录
- [ ]  在本页 §10 决策记录追加清理前后行数对比

**关键命令**

```bash
# 1. 计数 + 抽样
docker compose exec -T db psql -U postgres -d littlebox \
  -f backend/scripts/cleanup/m6_patch_cleanup.sql

# 2. 抽样核实后放开 DELETE 注释，重新跑（事务包裹）
docker compose exec -T db psql -U postgres -d littlebox \
  -f backend/scripts/cleanup/m6_patch_cleanup.sql

# 3. 验证残留 = 0
docker compose exec -T db psql -U postgres -d littlebox -c \
  "SELECT COUNT(*) FROM users WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$';"

# 4. Redis bind:* 残留计数 + 清理
docker compose exec -T redis redis-cli --scan --pattern 'bind:*' | wc -l
docker compose exec -T redis sh -c \
  "redis-cli --scan --pattern 'bind:*' | xargs -r redis-cli del"
docker compose exec -T redis redis-cli --scan --pattern 'bind:*' | wc -l   # 应为 0
```

**安全闸门**

- ❌ 抽样若发现非 e2e 特征样本（`admin_note` 不含 `'e2e-test parent'` 且 phone 不匹配 4 字母正则），**立即停止**回话讨论
- ✅ 仅在抽样 100% 命中 e2e 特征后才放开 DELETE
- ✅ DELETE 必须在 `BEGIN; ... COMMIT;` 事务内
- ✅ 清理后两次 COUNT 都为 0

**验证清单**

- ✅ 清理前 users 命中数已记录
- ✅ 抽样核实通过
- ✅ DELETE 在事务内执行成功
- ✅ 清理后 users 命中 = 0
- ✅ Redis `bind:*` key 数 = 0
- ✅ 本页 §10 已追加前后行数

**Commit**：`chore: execute m6-patch dev db cleanup (e2e-test residue)`

### Step 3 · CLI 业务函数签名重构（方案 C）

**任务**

- [ ]  `app/scripts/create_parent.py`：`_create_parent` 改签名为 `(db, redis, *, note) -> ParentInfo`，移除内部 `cli_runtime()`；新增 `_main()` 入口
- [ ]  `app/scripts/reset_parent_password.py`：同模板重构 `_reset_password(db, redis, *, phone) -> None` + `_main()`
- [ ]  在各脚本顶部定义 `ParentInfo` dataclass（phone / user_id / family_id）
- [ ]  `_common.py::cli_runtime` 不动
- [ ]  本机验证 `python -m app.scripts.create_parent --note "smoke-step3"` 跑通后 SELECT 确认 littlebox 写入 1 行，立即 DELETE 该行避免污染

**关键代码片段**

```python
# app/scripts/create_parent.py
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis

@dataclass(frozen=True)
class ParentInfo:
    phone: str
    user_id: int
    family_id: int

async def _create_parent(
    db: AsyncSession,
    redis: Redis,
    *,
    note: str,
) -> ParentInfo:
    """创建 parent 账户。CLI 与测试共用入口。"""
    phone = await _ensure_unique_phone(db)
    password = _generate_password()
    parent = User(phone=phone, password_hash=hash_pw(password), admin_note=note, role="parent")
    db.add(parent)
    await db.flush()
    family = Family(...)
    db.add(family)
    db.add(FamilyMember(user_id=parent.id, family_id=family.id, role="owner"))
    await commit_with_redis(db, redis)
    print(f"phone={phone} password={password}")
    return ParentInfo(phone=phone, user_id=parent.id, family_id=family.id)

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--note", required=True)
    return parser.parse_args()

async def _main() -> None:
    args = _parse_args()
    async with cli_runtime() as (db, redis):
        await _create_parent(db, redis, note=args.note)

if __name__ == "__main__":
    asyncio.run(_main())
```

`reset_parent_password.py` 同模板。

**验证清单**

- ✅ `_create_parent` / `_reset_password` 签名强制接收 `(db, redis, *, ...)`，无默认值
- ✅ `_main()` 极薄（仅 parse_args + cli_runtime + 调业务函数）
- ✅ `python -m app.scripts.create_parent --note "smoke-step3"` 本机跑通，phone/password 输出正常
- ✅ 验证完用 SQL 立即清掉本次写入的脏行
- ✅ `basedpyright` 无新增类型错误

**Commit**：`refactor(scripts): split CLI business logic from runtime for testability`

### Step 4 · 重写 `test_scripts.py`

**任务**

- [ ]  删除所有 `subprocess.run([sys.executable, "-c", ...])` + 内联 `create_async_engine`
- [ ]  删除 `_TEST_DB_URL` 拼接、所有 `os.environ.get("LB_DB")` 直接拼接
- [ ]  改为 `import _create_parent` / `_reset_password` + `db_session` / `redis_client` fixture
- [ ]  每条用例 docstring 用 Given/When/Then
- [ ]  保留 1 个 CLI 入口 smoke：`monkeypatch sys.argv` + `monkeypatch cli_runtime` 为产出 fixture 资源的 async ctx，验证 `_main()` 经 capsys 输出 `phone=`

**关键代码片段**

```python
# backend/tests/test_scripts.py
import pytest
from contextlib import asynccontextmanager
from app.scripts.create_parent import _create_parent, _main as create_parent_main
from app.scripts.reset_parent_password import _reset_password

class TestCreateParent:
    @pytest.mark.asyncio
    async def test_creates_parent_and_family(self, db_session, redis_client):
        """
        Given: 干净的测试库 + fakeredis
        When: 调用 _create_parent
        Then: ParentInfo 返回; users / families / family_members 各写入 1 行; phone 为 4 字母
        """
        info = await _create_parent(db_session, redis_client, note="unit-test")
        assert len(info.phone) == 4
        # ... db_session 断言

class TestCliEntrypoint:
    @pytest.mark.asyncio
    async def test_main_parses_note_and_invokes(
        self, monkeypatch, capsys, db_session, redis_client,
    ):
        """
        Given: argv 含 --note, cli_runtime 被 monkeypatch 成产出测试 fixture
        When: 调用 _main()
        Then: stdout 含 phone= 输出
        """
        monkeypatch.setattr("sys.argv", ["create_parent", "--note", "smoke"])

        @asynccontextmanager
        async def _fake_runtime():
            yield (db_session, redis_client)
        monkeypatch.setattr(
            "app.scripts.create_parent.cli_runtime", _fake_runtime,
        )

        await create_parent_main()
        assert "phone=" in capsys.readouterr().out
```

**验证清单**

- ✅ 文件内零 `subprocess` 调用
- ✅ 文件内零 `create_async_engine`
- ✅ 文件内零 `os.environ.get("LB_DB")` 直接拼接
- ✅ 所有用例 docstring 用 Given/When/Then
- ✅ `pytest backend/tests/test_scripts.py -v` 通过

**Commit**：`test(scripts): rewrite to use conftest fixtures, add CLI entry smoke`

### Step 5 · 重写 `test_e2e_auth.py`

**任务**

- [ ]  删除 `_BASE_URL` 常量、`_run_create_parent` / `_run_reset_password` subprocess 函数、`_flush_redis()` autouse fixture
- [ ]  CLI 段改 `await _create_parent(db_session, redis_client, ...)` / `await _reset_password(...)`
- [ ]  HTTP 段改 `api_client` fixture（ASGI in-process）
- [ ]  DB 校验段改用 `db_session` 直接查询断言（替代 subprocess + 内联 engine 验证）
- [ ]  每条用例 docstring 用 Given/When/Then
- [ ]  文件顶部 docstring 更新：删除「littlebox DB + redis://redis:6379/0」描述，改为「ASGI in-process，完全经 conftest 隔离」

**验证清单**

- ✅ 文件内零 `subprocess`
- ✅ 文件内零 `httpx.Client(base_url="http://localhost:8000")`
- ✅ 文件内零 `redis.Redis(host="redis"...)` 真连
- ✅ 文件内零 `flushdb`
- ✅ `pytest backend/tests/test_e2e_auth.py -v` 通过
- ✅ 跑完后 `littlebox` 库 `users` / `families` / `family_members` 行数与跑前一致（手动 SELECT 验证；Step 6 完成后由 fixture 自动验证）

**Commit**：`test(e2e-auth): rewrite to ASGI in-process with conftest fixtures`

### Step 6 · T4 防御加固（双层运行时保护）

**任务**

- [ ]  (a) `backend/tests/conftest.py` 顶部 import 阶段加模块级 fail-fast 断言
- [ ]  (b) 新增 session autouse fixture `_prod_db_row_count_guard` 比对真库 baseline 行数
- [ ]  加 env switch `LB_SKIP_PROD_GUARD=1` 用于 CI 跳过（默认开启）
- [ ]  为兜底 fixture 自身写 1 个 happy path 测试（确认正常跑测试时 baseline 不变）
- [ ]  故意制造污染场景验证防御能拦下（验证完恢复）

**关键代码片段**

```python
# backend/tests/conftest.py（顶部）
import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from app.config import settings

TEST_DB_NAME = "littlebox_test"

def _test_url() -> str:
    return (
        make_url(settings.database_url)
        .set(database=TEST_DB_NAME)
        .render_as_string(hide_password=False)
    )

# (a) 模块级 fail-fast 双断言
_RESOLVED_TEST_URL = _test_url()
assert "_test" in _RESOLVED_TEST_URL, (
    f"FATAL: 测试库 URL 必须含 '_test', 实际 {_RESOLVED_TEST_URL}。"
    f"请检查 settings.database_url 与 TEST_DB_NAME。"
)
assert "_test" in settings.database_url or settings.database_url == _RESOLVED_TEST_URL, (
    "FATAL: settings.database_url 与 _test_url() 关系异常,可能误连生产库。"
)

# (b) session autouse 行数兜底
_GUARD_TABLES = ["users", "families", "family_members"]

def _count_rows(url: str, tables: list[str]) -> dict[str, int]:
    sync_url = make_url(url).set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)
    eng = create_engine(sync_url)
    with eng.connect() as conn:
        return {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one() for t in tables}

@pytest.fixture(scope="session", autouse=True)
def _prod_db_row_count_guard():
    """启动记录真库 baseline, session 结束比对。任一目标表行数变化即 fail。"""
    if os.getenv("LB_SKIP_PROD_GUARD") == "1":
        yield
        return

    prod_url = settings.database_url   # 不走 _test_url(), 故意监控真库
    baseline = _count_rows(prod_url, _GUARD_TABLES)
    yield
    final = _count_rows(prod_url, _GUARD_TABLES)
    diffs = {t: (baseline[t], final[t]) for t in _GUARD_TABLES if baseline[t] != final[t]}
    assert not diffs, f"FATAL: 真库行数变化(测试污染): {diffs}"
```

**验证清单**

- ✅ 故意把 `TEST_DB_NAME` 改为 `littlebox_dev`（无 `_test`），pytest 立即 abort 并报清晰错误（验证后改回）
- ✅ 故意在测试里写一行直接 commit 到真库 `users`，session 结束兜底 fixture fail 并提示行数差（验证后删除）
- ✅ 正常跑 `pytest backend/tests/ -x` 全套通过，兜底 fixture 不报错
- ✅ `LB_SKIP_PROD_GUARD=1 pytest backend/tests/ -x` 也能正常跑（兜底跳过）

**Commit**：`test(conftest): add fail-fast url assertion and prod db row count guard`

### Step 7 · 文档与纪律

**任务**

- [ ]  `backend/tests/conftest.py` 顶部加模块 docstring，明示测试隔离铁律 + 黑名单 + 历史链接
- [ ]  `backend/CLAUDE.md` 测试章节新增「测试隔离铁律」小节，内容对齐 [Agent 指引 · 实施计划编写](https://www.notion.so/Agent-8edba833b10344dcbb5feb9193161952?pvs=21) §6.7
- [ ]  跑 `pytest backend/tests/ -x` 全量验证最终状态（通过数 = 改前 + Step 4/6 新增）
- [ ]  在本页 §10 决策记录补充最终通过情况、清理前后行数、新增 commit 列表
- [ ]  推分支并开 PR 入 main

**关键文档片段**

```python
# backend/tests/conftest.py 顶部
"""
测试隔离铁律(M6-patch 后强制纪律)

所有涉及 DB / Redis 的测试**必须**通过本文件的 fixture 进入:
- DB: db_session (savepoint rollback, 作用域 function)
- HTTP: api_client (ASGI in-process)
- Redis: redis_client (fakeredis, 作用域 function)
- 依赖注入: app.dependency_overrides

禁止:
- subprocess 跑 `app.scripts.*` 连真实库
- httpx 直连真 server (localhost:8000 等)
- redis.Redis(...) 显式连真实 host
- from app.config import settings 后用 settings.database_url 自建 engine
- flushdb() / flushall()

双层运行时防御:
- 模块级 _test_url() 断言 (本文件顶部)
- session 级 _prod_db_row_count_guard fixture

历史教训: M6-patch · 测试隔离纪律加固
"""
```

**验证清单**

- ✅ `backend/tests/conftest.py` 顶部 docstring 完成
- ✅ `backend/CLAUDE.md` 测试章节新增小节
- ✅ `pytest backend/tests/ -x` 全量通过（数量 = 改前 + 新增）
- ✅ `basedpyright` 全量通过
- ✅ 本页 §10 已记录最终结果
- ✅ PR 已开

**Commit**：`docs(tests): add test isolation discipline to conftest and CLAUDE.md`

## 7. 整体验收清单

- ✅ 7 步全部完成，各 commit 入 `fix/m6-patch-test-isolation` 分支
- ✅ `backend/tests/test_e2e_auth.py` 内零 subprocess / 零 httpx 直连 / 零 redis 真连 / 零 flushdb
- ✅ `backend/tests/test_scripts.py` 内零 subprocess / 零 create_async_engine / 零 settings.database_url 自建
- ✅ `app/scripts/create_parent.py` / `reset_parent_password.py` 业务函数强制注入 db/redis，生产 CLI 命令不变
- ✅ `pytest backend/tests/ -x` 全量通过
- ✅ 跑完测试套件后真库 `littlebox` 行数零变化（由 `_prod_db_row_count_guard` 自动验证）
- ✅ 故意制造的污染场景能被双层防御拦下（Step 6 验证已做）
- ✅ `backend/CLAUDE.md` 已加入测试隔离铁律小节
- ✅ PR 入 main

## 8. 不在本 patch 内做的事（重申）

- 不改 `conftest.py` 现有 fixture 实现
- 不引入 pytest-xdist
- 不动 frontend e2e
- 不为「真容器 + 真 server」保真度建独立 `littlebox_e2e` 库（如未来确需另开 patch）
- 不改 `cli_runtime()` 实现
- 不重构 `chat/` 子目录测试
- 不做 pre-commit 钩子

## 9. 发现与建议

阶段 1 核查中识别的额外信息，留作后续参考：

1. **CLI 业务/IO 未拆分**是测试隔离破口的根因之一。本 patch 后，新增 `app/scripts/*` 必须遵循方案 C 模板（强制注入 + 极薄 `_main`），由 `backend/CLAUDE.md` 测试隔离铁律小节同步登记
2. **BindToken 是纯 Redis（`bind:*` prefix），无 DB 表**。后续涉及 bind 的测试必须用 `redis_client` fixture，不要试图查 `bind_tokens` 表（不存在）
3. **`users.admin_note` 字段名**（非 `note`）。涉及该字段的查询 / 测试 / 文档需统一
4. **`chat/` 子目录测试未在本 patch 范围**（核查确认无 subprocess / httpx 直连 / redis 真连 破口），但 `test_locks` / `test_graph` / `test_persistence` / `test_sse` / `test_stream_chat_e2e` / `api/` 子目录未深读，后续若发现其他破口可独立 patch 修复
5. **`assert not pending` redis ops 护栏**（`conftest.py:142-145`）是已有保护，与新增的双层防御互补，不冲突

## 10. 决策记录

- **D1–D11** 已与 Iver 对齐（见 §3）
- **阶段 1 核查报告**已确认两破口位置与影响范围
- **数据清理前后行数**：（Step 2 完成后由执行 agent 填写）
- **最终测试通过情况**：（Step 7 完成后由执行 agent 填写）
- **`backend/CLAUDE.md` 测试纪律小节**：Step 7 由执行 agent 起草后回写本对话，规划 agent 复核

## 11. 关联

- 上游设计基线：[M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21)
- 主路径计划：[M6 · 主对话链路 - 后端核心 — 实施计划 (6/17)](https://www.notion.so/M6-6-17-a36bdd99fc0f445d86623025c330ea0c?pvs=21)
- 后续待办登记：[](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)
- 实施计划纪律（本 patch 完成后已纳入「测试隔离铁律」条款）：[Agent 指引 · 实施计划编写](https://www.notion.so/Agent-8edba833b10344dcbb5feb9193161952?pvs=21)