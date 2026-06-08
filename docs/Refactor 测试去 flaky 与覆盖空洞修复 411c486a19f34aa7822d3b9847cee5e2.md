# Refactor: 测试去 flaky 与覆盖空洞修复

<aside>
🎯

**性质**：测试可靠性修复（de-flake + 补覆盖空洞），提速是副产物，不是目标。

**分支基线**：从当前分支 `refactor/backend-audit-phase-1` HEAD 拉一个额外的重构分支 `test/dedupe-sleeps-and-coverage-holes` **串行**开发（不起 worktree），完成后合并回 `refactor/backend-audit-phase-1`。

**约束**：不碰 app 代码、不碰 `conftest.py` 结构、不引新依赖、不动隔离纪律。

**预期**：对应测试耗时归零（参考 -~11s），`never awaited` RuntimeWarning 7 条 → 0；passed / skipped 数与 `refactor/backend-audit-phase-1` 基线一致（基线数以 Step 1 实测为准，不假定 737/132——重构可能已改变用例集）。

</aside>

## 目标概述

**做什么**

- **P0**：移除 3 处真实 `asyncio.sleep`（1 处秒级精度等待 + 2 处重试退避），去 flaky 并省 ~11s。
- **B**：修 2 类 `coroutine ... never awaited` 泄漏——这是**绿测试里没真正执行到的覆盖空洞**，不是静音 warning。

**不做什么（本轮显式排除，冻结到 backend 大重构后）**

- engine fixture 池化 / session scope
- redis_client fixture 提 scope
- `create_app()` 复用
- 测试文件 lazy import
- pytest-xdist
- `filterwarnings` 强回归门
- 266 条上游 `iscoroutinefunction` DeprecationWarning 的静音（属上游 LangChain，本轮不碰）

## 前置条件

- [ ]  从当前 `refactor/backend-audit-phase-1` HEAD 拉分支(无需等其它重构完成)。
- [ ]  容器在跑：`docker compose up -d`；后续命令均在容器内执行（`docker compose exec api ...`，工作目录为 backend app 根，pytest 路径以 `tests/` 起）。
- [ ]  基线复测为绿（见 Step 1）。
- 环境事实（勿误判）：运行时 Python 3.14；LLM 为 DeepSeek V4 系列；无宿主机 Python，一切走容器。

## 执行步骤

### Step 1 · 建分支 + 基线画像

- [ ]  从当前 `refactor/backend-audit-phase-1` HEAD 拉额外分支:

```bash
git checkout -b test/dedupe-sleeps-and-coverage-holes
```

- [ ]  跑基线并记录：总耗时、最慢 20 项、warning 数

```bash
docker compose exec api pytest -q --durations=20 -rw
```

**验证清单**

- ✅ 记录 `refactor/backend-audit-phase-1` 上的基线 passed / skipped 数（不假定 737/132——重构可能已改变用例集）
- ✅ `--durations` 能看到 `test_ordering_by_created_at` ~6s、两个 `TestAuditLlmRetry::*` ~3.8s/~1.2s
- ✅ warnings summary 含 `never awaited`（错误源：`chat/graph.py` 的 `db.add(msg)` ×6 + lifecycle 测试的 `_run_llm_pipeline` ×1）
- 本步无代码提交（仅建分支 + 记录基线）

---

### Step 2 · P0-A1：children 排序测试去 sleep

`tests/test_children_list.py::test_ordering_by_created_at` 用 `await asyncio.sleep(3.0)` ×2 制造 `created_at` 秒级差。改为**复用同文件 `test_ordering_secondary_by_child_profile_id_when_created_at_equal` 已有的 `UPDATE child_profiles SET created_at = ...` 写法**直接回填时间戳，断言与场景不变。

- [ ]  删两处 `asyncio.sleep(3.0)`，改用显式回填 `created_at`
- [ ]  docstring 保持 Given/When/Then

```python
# 示意（列名 / 会话名以实际测试为准，镜像 sibling 测试写法）：
from datetime import datetime, timedelta, UTC
from sqlalchemy import update

base = datetime.now(UTC)
await db_session.execute(
    update(ChildProfile).where(ChildProfile.child_user_id == first_id)
    .values(created_at=base)
)
await db_session.execute(
    update(ChildProfile).where(ChildProfile.child_user_id == second_id)
    .values(created_at=base + timedelta(seconds=1))
)
await db_session.commit()
```

**验证清单**

- ✅ `docker compose exec api pytest tests/test_children_list.py -q` 全绿
- ✅ 该测试 `--durations` 从 ~6s 降到 <0.1s
- ✅ 排序断言（按 created_at）未变
- commit：`test: replace real sleeps with created_at backfill in children ordering test`

---

### Step 3 · P0-A2/A3：audit 重试测试去退避 sleep

`tests/chat/test_factory.py::TestAuditLlmRetry` 的两个测试走 `build_audit_llm` 的 `with_retry(wait_exponential_jitter=True, stop_after_attempt=3)`，真实耗时来自重试间的指数退避 `await asyncio.sleep(...)`。**只在测试层 mock 掉退避 sleep，不动 factory 的 retry 配置**，保留 respx 注入与重试次数断言（重试逻辑本身照测）。

- [ ]  顶部补 `import asyncio`
- [ ]  加一个 `monkeypatch` fixture 把退避 sleep 置 no-op，挂到两个测试上

```python
@pytest.fixture
def _no_retry_backoff(monkeypatch):
    """Given 重试退避会真实 sleep，When 置 asyncio.sleep 为 no-op，Then 重试逻辑不变但不耗时。"""
    from unittest.mock import AsyncMock
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    # 兜底：若 tenacity 直接引用了自身模块内 sleep，再补一行 patch（raising=False）
```

- [ ]  `test_primary_retry_on_connect_error_then_success(self, _no_retry_backoff)` —— 保留 `assert len(respx_mock.calls) == 2`
- [ ]  `test_primary_all_fail_uses_fallback(self, _no_retry_backoff)` —— 保留主端 3 次失败 → fallback 成功断言

**验证清单**

- ✅ `docker compose exec api pytest tests/chat/test_factory.py -q` 全绿
- ✅ 两测试 `--durations` 降到 <0.1s
- ✅ 重试次数 / fallback 行为断言未削弱（仅去掉等待时间）
- commit：`test: stub exponential backoff sleep in audit llm retry tests`

---

### Step 4 · B1：修 db.add 的 AsyncMock 协程泄漏（`chat/graph.py` 的 `db.add(msg)` ×6）

**锁定错误点（用锦点，不用行号）**：`chat/graph.py` 中的 `db.add(msg)` 调用（`rg "db.add(msg)" backend/app/chat/graph.py` 定位，或由警告 `coroutine 'AsyncMockMixin._execute_mock_call' was never awaited` 反查）。该调用在某些 chat 测试里因 db 会话被整库 `AsyncMock` 而返回一个**未被 await 的 coroutine**（`db.add` 在 SQLAlchemy 里是同步方法）。这意味着这些测试**没真正执行到 add 这步**——是覆盖空洞，必修，不是静音。

- [ ]  **先定位**来源测试：`docker compose exec api pytest -W "error::RuntimeWarning" -q` 让 never-awaited 升级为错误，失败节点即 6 条来源（很可能集中在 chat 图节点单测，db 被 AsyncMock 的那处 / 共享 helper）
- [ ]  把该 db mock 从整库 `AsyncMock()` 改为带 spec，让同步方法保持同步：

```python
# 反例：db = AsyncMock()  → db.add(...) 返回未 await 的 coroutine
from unittest.mock import MagicMock
from sqlalchemy.ext.asyncio import AsyncSession

db = MagicMock(spec=AsyncSession)   # add() 同步；commit/execute/flush 自动为 AsyncMock
# 若个别异步方法未被 spec 推断为 async，按需显式：db.commit = AsyncMock()
```

- [ ]  确认对 `db.add` 调用次数 / 入参的既有断言（若有）仍成立

**验证清单**

- ✅ 受影响测试模块全绿
- ✅ `pytest -rw` 中 `db.add(msg)` 处的 6 条 `never awaited` 归零
- ✅ `-W error::RuntimeWarning` 下该 6 条不再触发
- commit：`test: use AsyncSession-spec mock to fix db.add coroutine leak`

---

### Step 5 · B2：消费 lifecycle 注入测试的悬空协程（×1）

`tests/api/test_chat_stream_lifecycle.py::test_lock_released_on_non_http_exception_between_commit1_and_create_task` 把 `asyncio.create_task` patch 成在 `chat-llm-*` 命名时抛 `RuntimeError`，但传入的 `_run_llm_pipeline(...)` coroutine 在抛错前未被消费 → `never awaited`。在抛错前 `close()` 掉协程即可。

- [ ]  在注入桩里抛错前关闭协程

```python
def _raise_on_chat_llm(coro, *, name="", **kwargs):
    if name and name.startswith("chat-llm-"):
        coro.close()   # 消费掉协程，消除 never-awaited 泄漏
        raise RuntimeError("injected crash")
    return _orig_create_task(coro, name=name, **kwargs)
```

**验证清单**

- ✅ 该测试仍验证「注入崩溃后 except 块释放 session lock、Redis 无残留」
- ✅ `_run_llm_pipeline` 的 1 条 `never awaited` 归零
- commit：`test: close injected coroutine to fix _run_llm_pipeline never-awaited leak`

---

### Step 6 · 全量复测 + 合并回 `refactor/backend-audit-phase-1`

- [ ]  全量复测（默认跳过 live / integration）

```bash
docker compose exec api pytest -q --durations=20 -rw
```

- [ ]  推送并合并回 `refactor/backend-audit-phase-1`(串行执行、无并行重构,冲突应极少)

```bash
git push -u origin test/dedupe-sleeps-and-coverage-holes
git checkout refactor/backend-audit-phase-1 && git pull
git merge test/dedupe-sleeps-and-coverage-holes
```

- [ ]  如有冲突：保留本计划的修改意图（去真实 sleep / `AsyncSession`-spec mock / `coro.close()`），与重构对同文件的改动手工调和；解决后重跑全量复测。
- [ ]  合并后删分支前确认 `git log refactor/backend-audit-phase-1..test/dedupe-sleeps-and-coverage-holes` 为空（防提交丢失）。

**验证清单**

- ✅ passed / skipped 数与 Step 1 基线一致（无新增 skip）
- ✅ 对应测试耗时归零，总耗时较基线下降（参考 -~11s）
- ✅ warnings summary 中 `never awaited` 共 7 条 → 0
- ✅ 上游 `iscoroutinefunction` 的 266 条仍在（本轮不处理，符合预期）

## 验收清单（整体）

- ✅ 3 处真实 sleep 全部移除，对应测试耗时归零
- ✅ 7 条 `never awaited` RuntimeWarning 清零
- ✅ 测试通过数 / skip 数与基线一致，无新增 skip
- ✅ 未触碰 app 代码、`conftest.py` 结构、隔离纪律
- ✅ 4 个 commit 均符合 Conventional Commits
- ⏸ engine / redis / create_app / xdist / lazy import / filterwarnings 强门 —— 本轮不做，留待大重构后

## 发现与建议

- **重试测试归属校正**：`TestAuditLlmRetry` 实测的是 `build_audit_llm`（审查管线）而非 `build_main_llm`，profile 里的命名易误读为主对话链路；修法相同，仅在测试层去退避。
- **B1 定位手法**：`pytest -W "error::RuntimeWarning"` 是最快锁定 6 条来源的方式；若它们出自单一共享 db mock helper，则一处改完全收敛，否则按节点逐个改（均不影响隔离）。
- **上游 deprecation**：266 条 `iscoroutinefunction` 来自 LangChain，等上游修；将来若要清噪音，用窄范围 `filterwarnings` ignore 该条即可，**不要**顺手上 `["error", ...]` 强门（会逼出存量、不适合在大重构期做）。
- **分支与合并策略**:从当前 `refactor/backend-audit-phase-1` HEAD 拉额外分支 `test/dedupe-sleeps-and-coverage-holes`(不起 worktree),完成后合并回 `refactor/backend-audit-phase-1`。因串行执行、无并行重构,冲突应极少;但 `refactor/backend-audit-phase-1` 仍在演进,目标测试文件的行号 / 形态可能与本计划描述有出入——以执行时实际代码为准并回报。
- **与待办呼应**：本计划即父页「待办记录」中「测试时间过长，考虑压缩整体测试耗时」一项的最小落地切口（只取零风险部分）。