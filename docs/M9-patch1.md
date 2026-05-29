# Refactor: M9-patch1 · 主对话链路生命周期解耦

> 状态：**待执行** · 待 Iver 触发执行 agent
> 

> 关联设计基线：[M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21) §5.3 / §6.4 / §3.3 / §4.3
> 

> 前置 milestone：M9-patch0（图边界 + Runtime DI 校准）已合 main、M8 审查 pipeline 已收口、M6-patch3 测试隔离铁律已生效
> 

> 配套前端：[FE: M9-patch1 · flow_pause 前端识别清单](https://www.notion.so/FE-M9-patch1-flow_pause-bcc9e2eae76548f2ba3f16d94e25bb4b?pvs=21)（NoNo 结对编码，本计划合 main 后启动）
> 

> 编写规范：[Agent 指引 · 实施计划编写](https://www.notion.so/Agent-8edba833b10344dcbb5feb9193161952?pvs=21)
> 

> 触发来源：[M7 · 聊天界面前端 — 实施计划 (7/17)](https://www.notion.so/M7-7-17-e91264fbb7c44ba699ad71cc09964f47?pvs=21) 真机验证时，思考态强杀前端 app 触发——后端无 ai 行、lock 残留、前端 Waiting 撑到 200s 上限才 A4Late
> 

## 0 · 目标与范围

### 0.1 要做

把 `backend/app/api/me.py` 内 `chat_stream` 的 LLM consumption 协程从 HTTP request task 解耦成独立 `asyncio.create_task`（段一），StreamingResponse generator（段二）仅作为帧转发层。两段之间用有界 `asyncio.Queue(maxsize=128)` 单向中转，队满则段一翻 `state.overflow` flag、段二取前 check 主动发 `flow_pause` 帧让前端走 §6.4 Resume。

**段一职责范围（核验 main 后澄清）**：段一不仅承载 `graph.astream` 主循环，还需迁入现状 generator 内的**全部**业务逻辑——compression 整段（阻塞压缩 prompt + summarizer LLM + `compression_start` / `compression_end` / 失败 error 三种帧）、thinking 状态机（`reasoning` → `thinking_start`；首个非空 delta → `thinking_end`）、`stream_graph_to_sse` 帧映射、stop 双分支（StopWithAi 写 ai 行 + `enqueue_audit` 推带 aid 的 stopped 帧 / StopNoAi 仅推不带 aid 的 stopped 帧）、commit② 段（ai 行 + `usage_meta` 快照 + `context_size_tokens` + `needs_compression` 翻转 + `enqueue_audit`）。段二仅保留「取帧 → check overflow → yield → 客户端断检测」四件事。

### 0.2 不做

- ❌ ARQ worker / 跨进程 chat 任务调度（与 §3.3 单 worker + in-process running_streams 冲突）
- ❌ Redis Pub/Sub 跨进程帧广播
- ❌ `asyncio.shield` 局部保护（治标不治本，CancelledError 是 task 级不可逆）
- ❌ 段二降速钩子（与断流机制互斥）
- ❌ 水位线 HWM/LWM（单生产者顺序 put 场景无需）
- ❌ 真正触发降速（`chat_stream_interval_s` 默认 0.0 占位）
- ❌ 日轮次硬控（剥离为独立 issue，本 patch 不做）
- ❌ 段二在 sseClient 层 close（前端只透传类型，由 store 决定 close 与 Resume）

### 0.3 核心 bug 与根因（背景，不在 Step 内重述）

**现象**：思考态强杀前端 app，再回到 app 时后端没有 ai 行、session lock 残留、前端 Resume 拉到 `in_progress=true` 一直 Waiting 直到防御上限（200s）才落 A4Late。

**根因时序**：

1. Starlette 检测到 TCP RST → 直接 cancel **整个 HTTP request task**
2. asyncio cancellation 是 **task 级不可逆**——该 task 内所有 await 重新抛 CancelledError
3. `graph.astream.__anext__()` / `db.commit` / `release_session_lock` 全部被 cancel
4. `except CancelledError: client_alive = False` 形同虚设（异常向上传播）
5. **commit② 段（落 ai 行 + enqueue_audit + needs_compression）来不及执行**
6. `finally` 里 `await release_session_lock` 也被 cancel → **lock 残留**
7. lock 残留撑到 180s TTL 自然过期或前端 200s 防御上限

**本质**：违背 §5.3「不 cancel 原则」契约。

---

## 1 · 前置条件（执行前 sanity）

执行 agent 启动前确认：

- [ ]  `main` 分支已合入 M9-patch0（图工厂化、`rr.main_graph` 可用、`_MainGraphCompat` 测试兼容层可拆）
- [ ]  `main` 分支已合入 M8 审查 pipeline（`enqueue_audit` 接口稳定，from `app.chat.graph`，签名 `(sid, db, turn_number, child_user_id)`）
- [ ]  `main` 分支已合入 M6-patch3（[conftest.py](http://conftest.py) 测试隔离 fixture 齐备）
- [ ]  本地 `docker compose up` 起得来，`docker compose exec api pytest -q` 全绿（基线 sanity）

任一项不满足，停止执行并向 Iver 报告。

---

## 2 · 关键决策表（已收口）

讨论草案 §11 的 6 个开放项与后续追问已逐项收口，执行 agent 直接按下表取值，**不再回讨论**：

| 决策点 | 取值 | 位置 |
| --- | --- | --- |
| queue maxsize | 128（settings 默认） | `settings.chat_queue_maxsize` |
| shutdown 等候超时 | 30 秒 | `main.py` lifespan |
| 降速钩子默认值 | 0.0（占位，不触发） | `settings.chat_stream_interval_s` |
| 降速钩子位置 | 段一循环（控生产） | `_run_llm_pipeline` |
| 段一异常路径 | 显式 `await db.rollback()`  • **不写 ai 占位行**  • 推 error 帧 + release lock | 段一 except 分支 |
| §6.4 异常归类 | (false, human) → A4Late（前端不需特殊处理） | 设计基线对齐 |
| 前端识别 `flow_pause` | **本 patch 必做**（配套 FE 清单页 NoNo 主导） | FE 清单页 |
| `running_streams` 注册时机 | **`asyncio.create_task` 之前**（避免段一进循环时 race） | HTTP handler 同步段 |
| 段一 DB session | **自建** via `rr.db_session_factory()`，不持 request db 句柄 | 段一 `async with` |
| 段二 client 断检测 | `yield` 抛 `ConnectionError` / `BrokenResourceError` 静默退出 | 段二 except |
| 日轮次硬控 | **本 patch 不做**（独立 issue 跟踪） | — |
| 分支命名 | `refactor/m9-patch1-stream-lifecycle` | git |

---

## 3 · 改动清单（执行 agent 视角）

| 文件 | 改动 |
| --- | --- |
| `backend/app/runtime.py` | 扩 `RuntimeResources`：`db_session_factory` / `_chat_tasks` (default_factory) / `register_chat_task`；扩 settings：`chat_queue_maxsize=128` / `chat_stream_interval_s=0.0` |
| `backend/app/chat/sse.py` | 新增 `build_flow_pause_frame(reason: str) -> bytes` |
| `backend/app/api/me.py` | **核心改动**：`chat_stream` 拆为 `_run_llm_pipeline`（段一 bg task）+ `_stream_generator`（段二）；新增 `_ChatStreamState` dataclass；**先把现状 generator 内 inline 块抽为独立 helper（commit② / compression / thinking 状态机 / stream_graph_to_sse 包装 / stop 双分支），再迁段一**——这些 helper 在 main 上目前不存在；`enqueue_audit` 真实签名 `(sid, db, turn_number, child_user_id)`，按现状直调即可；将顶层 `try ... except HTTPException` 兜底**扩展为覆盖任意异常**（详见 §7.3 现状对照），封死 commit① 与 `create_task` 之间的 lock 残留另一条路径；删除原 `except ConnectionError, anyio.BrokenResourceError, asyncio.CancelledError: client_alive = False` 形同虚设代码；删除 `_MainGraphCompat` 与模块级 `_main_graph` 占位（grep `app.api.me._main_graph` 改用 `rr.main_graph`，patch0 已工厂化）；段一 finally 中 `release_session_lock` 异常分支改为 `logger.warning("release lock failed, rely on TTL", exc_info=True, extra={"sid": str(sid)})` 留痕（不再 `pass` 静默吃异常） |
| `backend/app/main.py` | lifespan shutdown 阶段 `asyncio.wait(_chat_tasks, timeout=30)`  • 超时 cancel |
| `backend/app/chat/locks.py` | **无改动**（`running_streams` / acquire / release 接口不变） |
| `backend/tests/api/test_chat_stream.py`（新建或扩） | 8 个 Given/When/Then 测试覆盖正常流 / 客户端断连 / queue full / 段一异常 / shutdown 等候 / shutdown 超时 cancel / stop 信号 / commit① 与 create_task 之间异常的 lock 释放 |
| 前端 | 由 [FE: M9-patch1 · flow_pause 前端识别清单](https://www.notion.so/FE-M9-patch1-flow_pause-bcc9e2eae76548f2ba3f16d94e25bb4b?pvs=21) 在本计划合 main 后并行落地（不在本计划 Step 内） |

**新增 SSE 帧 schema**：`{"type": "flow_pause", "reason": "backpressure"}`。

---

## 4 · Step 1 · 切分支 + 基线 sanity

### 4.1 目标

建立 patch1 工作分支，跑通基线测试，确认前置条件满足。

### 4.2 涉及文件

无（仅 git 操作）。

### 4.3 操作

```bash
git checkout main
git pull --rebase
git checkout -b refactor/m9-patch1-stream-lifecycle
docker compose exec api pytest -q
```

### 4.4 验证

- [ ]  分支已建
- [ ]  基线 `pytest -q` 全绿
- [ ]  报告 Iver 进入 Step 2

---

## 5 · Step 2 · 扩 RuntimeResources 与 settings

### 5.1 目标

为段一 bg task 提供独立 DB session factory、task 登记表、可配置 queue 上限与降速钩子开关；为 lifespan shutdown 提供 task 集合句柄。

### 5.2 涉及文件与改动

`backend/app/runtime.py`：

- 字段 `db_session_factory: async_sessionmaker[AsyncSession]`（lifespan 初始化时传入，已存在 engine 直接绑）
- 字段 `_chat_tasks: dict[str, asyncio.Task] = field(default_factory=dict)` ⚠️ **必须用 default_factory**，class-level `= {}` 是 Python mutable default 大坑（所有实例共享同一 dict，测试串数据）
- 方法 `register_chat_task(sid: str, task: asyncio.Task)` 内带 `add_done_callback` 自动 pop + 记录未捕获异常
- settings 字段 `chat_queue_maxsize: int = 128`
- settings 字段 `chat_stream_interval_s: float = 0.0`（占位，注释说明 > 0 时段一每帧 sleep）

### 5.3 代码骨架

```python
from dataclasses import dataclass, field
import asyncio
import logging

logger = logging.getLogger(__name__)

@dataclass
class RuntimeResources:
    # ... 既有字段保持不变 ...
    db_session_factory: "async_sessionmaker[AsyncSession]"
    _chat_tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def register_chat_task(self, sid: str, task: asyncio.Task) -> None:
        self._chat_tasks[sid] = task

        def _on_done(t: asyncio.Task) -> None:
            self._chat_tasks.pop(sid, None)
            # bg task 异常默认被静默吞掉（仅 GC 时 log warning，不可靠）
            if not t.cancelled():
                if exc := t.exception():
                    logger.error(
                        "chat task crashed unhandled",
                        extra={"sid": sid},
                        exc_info=exc,
                    )
        task.add_done_callback(_on_done)
```

settings 同步扩：

```python
class Settings(BaseSettings):
    # ... 既有字段 ...
    chat_queue_maxsize: int = 128
    chat_stream_interval_s: float = 0.0  # > 0 时段一每帧 sleep，占位钩子
```

### 5.4 验证

新增测试 `backend/tests/runtime/test_chat_task_registry.py`（Given/When/Then）：

```python
async def test_register_chat_task_auto_pops_on_done(runtime_resources):
    """
    Given a registered chat task,
    When the task finishes normally,
    Then it should be auto-removed from _chat_tasks via done_callback.
    """
    # ...

async def test_register_chat_task_logs_unhandled_exception(
    runtime_resources, caplog
):
    """
    Given a chat task that raises,
    When the task completes,
    Then the unhandled exception should be logged with sid context.
    """
    # ...

def test_chat_tasks_default_factory_isolated():
    """
    Given two RuntimeResources instances,
    When each registers a task,
    Then their _chat_tasks dicts should be independent (no shared mutable).
    """
    # ...
```

- [ ]  三个测试全绿
- [ ]  `docker compose exec api pytest -q` 全绿
- [ ]  commit：`feat(runtime): add chat task registry and queue/interval settings`

---

## 6 · Step 3 · 新增 `build_flow_pause_frame`

### 6.1 目标

在 `backend/app/chat/sse.py` 暴露 `flow_pause` 帧构造函数，与既有 `build_start_frame` / `build_delta_frame` / `build_end_frame` / `build_error_frame` 风格一致。

### 6.2 涉及文件与改动

`backend/app/chat/sse.py`：新增 `build_flow_pause_frame(reason: str = "backpressure") -> bytes`。

### 6.3 代码骨架

```python
def build_flow_pause_frame(reason: str = "backpressure") -> bytes:
    """Frame emitted by stream generator when backend triggers a graceful cutoff.

    Front-end (see FE checklist) recognizes this frame, closes SSE, and triggers
    the existing Resume channel.
    """
    payload = {"type": "flow_pause", "reason": reason}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
```

### 6.4 验证

新增 `backend/tests/chat/test_sse_frames.py` 内追加：

```python
def test_build_flow_pause_frame_default_reason():
    """
    Given the default backpressure reason,
    When building a flow_pause frame,
    Then the bytes should encode the canonical schema.
    """
    frame = build_flow_pause_frame()
    assert b"flow_pause" in frame
    assert b"backpressure" in frame
    # 完整 schema 断言 ...
```

- [ ]  测试全绿
- [ ]  commit：`feat(sse): add flow_pause frame builder`

---

## 7 · Step 4 · 拆 `chat_stream` 为段一段二（核心改动）

### 7.1 目标

把 `chat_stream` 内 LLM consumption 协程从 HTTP request task 解耦，由独立 `asyncio.create_task`（段一）承担生命周期；StreamingResponse generator（段二）退化为帧转发 + overflow check。

### 7.2 涉及文件与改动

`backend/app/api/me.py`：

- 新增私有 dataclass `_ChatStreamState`（仅 `overflow: bool = False`）
- 新增私有 async 函数 `_run_llm_pipeline(rr, sid, nonce, initial_state, ctx, queue, state)`（段一）
- 新增私有 async generator `_stream_generator(queue, state, sid)`（段二）
- 重构 `chat_stream` 端点：同步前置不变，但在创建 bg task **之前**注册 `running_streams[str(sid)]`，再 `create_task` + `rr.register_chat_task`，最后返回 `StreamingResponse(_stream_generator(...))`
- 将顶层 `try ... except HTTPException` 兜底扩展为 `except Exception`（或同等效的 BaseException 子集），封死 commit① 与 `create_task` 之间非 HTTPException 异常绕过 `release_session_lock` 的 lock 残留路径（详见 §7.3 现状对照）
- 删除原 `except CancelledError: client_alive = False` 形同虚设代码
- 删除 `_MainGraphCompat` 测试兼容层（patch0 已完成图工厂化，测试改用 `rr.main_graph`）
- 段一 finally 中 `release_session_lock` 异常分支用 `logger.warning` 留痕，不再 `pass` 静默吃异常（详见 §7.4 骨架）

### 7.3 现状对照（main 分支实证核验）

main 上 `chat_stream` 的关键结构（与本 patch 重构相关的部分）：

```python
nonce = await acquire_session_lock(redis, str(sid))
if not nonce:
    raise HTTPException(409, "SessionBusy")

# ★ 现状兜底仅 catch HTTPException，任何非 HTTPException 异常（DB 连接 / OOM / Redis 暂态错误）
#   在 commit① 与 return StreamingResponse 之间抛出都会绕过 release_session_lock
#   → lock 残留 180s TTL，是孤儿 human bug 之外的另一条 lock 残留路径
try:
    # session ownership check / child_profile / decision matrix / commit①
    ...
    return StreamingResponse(generator(), media_type="text/event-stream")
except HTTPException:
    await release_session_lock(redis, str(sid), nonce)
    raise
```

现状 generator 内 inline 业务逻辑（**全部需迁段一**）：

1. **compression 整段**（`if session.needs_compression:`）：同步执行 summarizer LLM、UPDATE active → compressed、INSERT summary 行、手动重构 `initial_state["messages"]`、推 `compression_start` / `compression_end`；失败时推 error 帧后 `return`
2. **thinking 状态机**：`payload.get("reasoning")` 触发 `thinking_start`；首个非空 `delta` 触发 `thinking_end`；`thinking_started` + `client_alive` 双 flag 控制
3. **stop 顶部检查**：每个 chunk 进 for 顶部即 `event.is_set()` —— 保证 `has_emitted_content` / `accumulated` 仅反映已成功 yield 的 chunks（这条逻辑要原样搬到段一，且语义不变）
4. **stream_graph_to_sse 帧映射**：每个 payload 包裹 `_wrap()` async-gen 过映射器再 yield
5. **commit② 自然结束分支**：构造 `Message(role=ai, status=active, finish_reason=last_finish_reason)`、`db.flush()` 取 `aid`、`usage_meta` → `context_size_tokens` / `needs_compression` 翻转、`db.commit()`、`enqueue_audit(sid, db, _turn_number, current.id)`、推 `end` 帧带 `aid`
6. **commit② StopWithAi 分支**：与自然结束类似，但 `finish_reason="user_stopped"`，推 `stopped` 帧带 `aid`
7. **StopNoAi 分支**：不写 ai 行，仅推 `stopped` 帧不带 `aid`
8. **顶层 except Exception**：推 error 帧；**不回滚 human 行**（已 commit①），与本 patch §0.3 决策一致
9. **finally**：`running_streams.pop(str(sid), None)` → `release_session_lock(redis, str(sid), nonce)`

本 patch 重构后归位：1-8 整体迁段一（先抽 helper 后迁，避免一次性大改难审）；9 在段一 finally 重现；段二仅「取帧 / overflow check / yield / 客户端断」。

**`enqueue_audit` 签名澄清**：from `app.chat.graph import enqueue_audit`，签名 `async def enqueue_audit(sid, db, turn_number, child_user_id)`。Step 4 §7.4 骨架中的 `_enqueue_audit(rr.audit_redis, sid, ai_text)` 是伪占位，执行 agent 按真实签名直调（无需再抽 wrapper helper）。

### 7.4 代码骨架

```python
from dataclasses import dataclass

@dataclass
class _ChatStreamState:
    """段一段二共享的轻量 mutable container。仅含 overflow flag。"""
    overflow: bool = False

async def _run_llm_pipeline(
    rr: RuntimeResources,
    sid: UUID,
    nonce: str,
    initial_state: dict,
    ctx: Context,
    queue: asyncio.Queue,
    state: _ChatStreamState,
) -> None:
    interval_s = rr.settings.chat_stream_interval_s
    stop_event = running_streams[str(sid)]
    full_text_parts: list[str] = []
    end_reason = "completed"

    try:
        async with rr.db_session_factory() as db:
            try:
                async for payload in rr.main_graph.astream(
                    initial_state, context=ctx, stream_mode="custom"
                ):
                    if stop_event.is_set():
                        end_reason = "stopped"
                        break

                    delta = payload.get("delta", "")
                    full_text_parts.append(delta)

                    if not state.overflow:
                        try:
                            queue.put_nowait(build_delta_frame(payload))
                        except asyncio.QueueFull:
                            state.overflow = True
                            logger.info(
                                "queue overflow, headless mode",
                                extra={"sid": str(sid)},
                            )

                    if interval_s > 0:
                        await asyncio.sleep(interval_s)

                # commit② 段（含可能阻塞的压缩判断，归段一）
                ai_text = "".join(full_text_parts)
                await _commit_ai_row(db, sid, ai_text, end_reason)
                await _enqueue_audit(rr.audit_redis, sid, ai_text)
                await _maybe_mark_needs_compression(db, sid)

                if not state.overflow:
                    try:
                        queue.put_nowait(build_end_frame(end_reason))
                    except asyncio.QueueFull:
                        state.overflow = True

            except Exception as e:
                logger.exception(
                    "llm pipeline error", extra={"sid": str(sid)}
                )
                end_reason = "error"
                # ★ 关键决策：显式 rollback + 不写 ai 占位行
                # （§6.4 (false, human) → A4Late 路径，前端轮询命中常规分支）
                await db.rollback()
                if not state.overflow:
                    try:
                        queue.put_nowait(build_error_frame(str(e)))
                    except asyncio.QueueFull:
                        state.overflow = True

    finally:
        running_streams.pop(str(sid), None)
        if not state.overflow:
            try:
                queue.put_nowait(None)  # 哨兵
            except asyncio.QueueFull:
                state.overflow = True
        try:
            await release_session_lock(rr.redis, str(sid), nonce)
        except Exception:
            # lifespan shutdown 时 CancelledError 继承 BaseException 抓不住，
            # 仍向上传播；正常路径异常留痕后由进程级 180s TTL 兜底（§3.3 已认下）
            logger.warning(
                "release lock failed, rely on TTL",
                exc_info=True,
                extra={"sid": str(sid)},
            )

async def _stream_generator(
    queue: asyncio.Queue,
    state: _ChatStreamState,
    sid: UUID,
) -> AsyncIterator[bytes]:
    try:
        while True:
            # ★ check 在 await queue.get() 之前
            # asyncio 单线程下，yield 后控制权交 uvicorn 做 TCP write（弱网阻塞），
            # 段一拿到调度持续 put → 段二回 generator 时直接进入循环顶检测 flag，
            # 捕获「段一已发生 QueueFull」的真实状态。
            # 若放 get 之后检测 queue.full()，size 已减 1，永远 False。
            if state.overflow:
                yield build_flow_pause_frame("backpressure")
                logger.info(
                    "sse backpressure cutoff", extra={"sid": str(sid)}
                )
                return

            frame = await queue.get()
            if frame is None:
                break

            try:
                yield frame
            except (ConnectionError, anyio.BrokenResourceError):
                logger.info(
                    "client disconnected", extra={"sid": str(sid)}
                )
                return
    except asyncio.CancelledError:
        # 仅 lifespan shutdown 触发；段一在 lifespan 另有兜底
        raise

@router.post("/me/chat")
async def chat_stream(
    req: ChatRequest,
    rr: RuntimeResources = Depends(get_runtime),
    db: AsyncSession = Depends(get_db),  # request-scope，仅用于 commit①
):
    # 同步前置：限频、commit①、拿 session 锁、build initial_state
    # （既有逻辑不变，省略）
    nonce = await acquire_session_lock(rr.redis, str(sid))
    if not nonce:
        raise HTTPException(409, "SessionBusy")

    await _commit_human_row(db, sid, req)  # commit①

    # ★ 注意：commit① 与 create_task 之间任意 await 抛异常时，
    #   必须保证 release_session_lock(nonce) 仍被调用（既有 try/except 结构保留，
    #   否则 lock 残留 180s TTL）
    # ★ stop 通道注册必须在 create_task 之前（避免段一进循环时 race）
    stop_event = asyncio.Event()
    running_streams[str(sid)] = stop_event

    queue: asyncio.Queue = asyncio.Queue(
        maxsize=rr.settings.chat_queue_maxsize
    )
    state = _ChatStreamState()

    bg = asyncio.create_task(
        _run_llm_pipeline(rr, sid, nonce, initial_state, ctx, queue, state),
        name=f"chat-llm-{sid}",
    )
    rr.register_chat_task(str(sid), bg)

    return StreamingResponse(
        _stream_generator(queue, state, sid),
        media_type="text/event-stream",
    )
```

### 7.5 验证

Step 4 仅做编译 / lint sanity，完整测试在 Step 6 一次性补齐：

- [ ]  `docker compose exec api ruff check .` 无 error
- [ ]  `docker compose exec api mypy app` 无 error（或保持既有 baseline）
- [ ]  `docker compose exec api pytest -q` —— 既有测试可能因 `_MainGraphCompat` 删除而失败，允许红，Step 6 修复
- [ ]  commit：`refactor(chat): decouple llm pipeline from http request lifecycle`

---

## 8 · Step 5 · lifespan shutdown 30s 等候

### 8.1 目标

进程关闭时给段一 bg task 一次有界的优雅退出窗口，超时再 cancel。

### 8.2 涉及文件与改动

`backend/app/main.py`（或 lifespan 所在文件）。

### 8.3 代码骨架

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... startup（既有）...
    yield
    # shutdown
    tasks = list(rr._chat_tasks.values())
    if tasks:
        logger.info(
            "waiting chat tasks to finish",
            extra={"count": len(tasks)},
        )
        done, pending = await asyncio.wait(tasks, timeout=30.0)
        if pending:
            logger.warning(
                "chat tasks timeout, cancelling",
                extra={"pending": len(pending)},
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
```

### 8.4 验证

Step 6 整合测试一并覆盖。本步只做：

- [ ]  启动 / 停止 `docker compose` 服务，确认 lifespan 日志输出符合预期
- [ ]  commit：`feat(lifespan): wait chat tasks up to 30s on shutdown`

---

## 9 · Step 6 · 完整测试设计（8 个 Given/When/Then 用例）

### 9.1 目标

用 [conftest.py](http://conftest.py) 既有 fixture（`api_client` / `db_session` / `mock_redis` / `runtime_resources`），覆盖段一段二解耦后的所有关键路径。**严禁 subprocess / 真 server / 显式 `redis.Redis()` / `flushdb`**（M6-patch3 测试隔离铁律）。

### 9.2 涉及文件

`backend/tests/api/test_chat_stream_lifecycle.py`（新建）。

### 9.3 测试用例

```python
async def test_normal_stream_emits_deltas_and_end(api_client, runtime_resources):
    """
    Given a healthy LLM stub yielding 3 deltas,
    When the client consumes the SSE stream end-to-end,
    Then it should receive start + 3 deltas + end frame,
         the ai row should be committed,
         and the session lock should be released.
    """

async def test_client_disconnect_keeps_bg_task_running(
    api_client, runtime_resources
):
    """
    Given an in-progress chat stream,
    When the client closes the connection mid-stream,
    Then the bg task should continue, commit② should land the ai row,
         and release_session_lock should run.
    """

async def test_queue_full_triggers_flow_pause_and_headless_continuation(
    api_client, runtime_resources, monkeypatch
):
    """
    Given chat_queue_maxsize=4 and a slow consumer simulation,
    When the producer fills the queue,
    Then state.overflow flips True,
         the next frame yielded is flow_pause(reason=backpressure),
         the generator returns,
         and the bg task still commits the ai row + releases the lock.
    """

async def test_llm_pipeline_exception_rolls_back_without_ai_row(
    api_client, runtime_resources
):
    """
    Given an LLM stub that raises mid-stream,
    When the bg task hits the except branch,
    Then db.rollback() should be called explicitly,
         no ai placeholder row should be written,
         an error frame should be yielded (if state.overflow is False),
         and the session lock should still be released.
    """

async def test_shutdown_waits_for_in_flight_bg_task(
    runtime_resources, monkeypatch
):
    """
    Given an in-flight chat bg task expected to finish in 2s,
    When lifespan shutdown begins,
    Then asyncio.wait should return with done containing the task
         before the 30s timeout, and no task should be cancelled.
    """

async def test_shutdown_cancels_stuck_bg_task_after_timeout(
    runtime_resources, monkeypatch
):
    """
    Given a stuck chat bg task (sleep(60)),
    When lifespan shutdown times out at 30s,
    Then the pending task should be cancelled and gathered without raising.
    """

async def test_stop_signal_breaks_pipeline_with_stopped_end_reason(
    api_client, runtime_resources
):
    """
    Given an in-flight chat bg task,
    When running_streams[sid].set() is called externally,
    Then the pipeline should break with end_reason='stopped',
         commit② should land the partial ai_text,
         and the end frame should reflect 'stopped'.
    """

async def test_lock_released_on_non_http_exception_between_commit1_and_create_task(
    api_client, runtime_resources, monkeypatch
):
    """
    Given commit① and acquire_session_lock both succeeded,
    When the immediately following asyncio.create_task call raises RuntimeError
         (or any non-HTTPException between commit① and StreamingResponse return),
    Then release_session_lock(nonce) should still be called,
         and Redis chat:lock:<sid> should not be left dangling.
    """
```

### 9.4 验证

- [ ]  8 个新测试全绿
- [ ]  既有测试因 `_MainGraphCompat` 删除而红的全部修复（改用 `rr.main_graph`）
- [ ]  `docker compose exec api pytest -q` 全绿
- [ ]  commit：`test(chat): cover decoupled stream lifecycle scenarios`

---

## 10 · Step 7 · 与 FE 清单页协调真机回归

### 10.1 目标

配合 [FE: M9-patch1 · flow_pause 前端识别清单](https://www.notion.so/FE-M9-patch1-flow_pause-bcc9e2eae76548f2ba3f16d94e25bb4b?pvs=21) 完成端到端真机回归，确认孤儿 human bug 不再复现、新增 flow_pause 通路工作。

### 10.2 协议

本 Step 由 Iver + NoNo 主导，执行 agent 只负责按 FE 清单页要求临时调 settings（如 `chat_queue_maxsize=4`）配合弱网用例。

### 10.3 真机用例（与 FE 清单页 §7 表对齐）

1. 思考态强杀进程 + 立即重启 → ~5s 内 lock 释放、~10s 内 OK2 渲染整段 ai
2. 思考态强杀进程 + 隔 30s 重启 → 重启时段一已 commit②，直接 OK2 渲染
3. 弱网模拟（NLC Very Bad Network）+ `chat_queue_maxsize=4` → 收 flow_pause → Resume → OK2
4. 正常完整流（强网）→ 无 flow_pause，实时流式渲染
5. 用户主动 Stop → 段一 break，end_reason=stopped，DB 末态正确
6. dev lifespan reload（hot reload）→ 段一在跑时 reload，shutdown 30s 等候生效

### 10.4 验证

- [ ]  6 类用例全过
- [ ]  录屏归档到 FE 清单页 §7 表
- [ ]  settings 临时调整已 revert
- [ ]  Iver 签字确认 → 进 Step 8

---

## 11 · Step 8 · 文档回写与收尾

### 11.1 目标

把本 patch 的实现纪律沉淀到设计基线、登记执行偏差、关闭本计划页。

### 11.2 涉及文件

- [M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21) §5.3 实现纪律节追加一条：
    
    > **实现纪律（M9-patch1 沉淀）**：LLM consumption 协程必须用 `asyncio.create_task` 解耦于 HTTP request task；段二 StreamingResponse generator 仅作帧转发 + overflow check；中间用有界 `asyncio.Queue` 单向中转。**禁止把 LLM consumption 直接放在 StreamingResponse generator 内**。
    > 
- **新建** `M9-patch1 · 执行偏差记录` 子页（父级 = 本计划页，emoji 📋，格式参照 [M9-patch0 · 执行偏差记录](https://www.notion.so/M9-patch0-35b24688f16645f999a931bed37aa745?pvs=21)），把会话交接、环境适配、与计划不一致项、Step 7 真机用例录屏链接全部沉淀进去。
- [M7 · 执行偏差记录](https://www.notion.so/M7-4e0eef4cc09a43a08e2ec6b8dcd66ce9?pvs=21) Step 9 节追加交叉引用：
    
    > 真机回归发现孤儿 human bug → 剥离为独立专项 M9-patch1「主对话链路生命周期解耦」，详见该 patch 自身的执行偏差记录。
    > 
- [](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)：
    - 新增条目：**日轮次硬控**（剥离自 M9-patch1 §0.2 / §14.2，独立 issue 跟踪，本 patch 不实现）
    - 关闭条目：M9-patch1 主对话链路生命周期解耦
- 本计划页：状态从「待执行」改为「已合并」，并在顶部状态行追加 PR 链接
- 提醒 FE 清单页同步关闭

### 11.3 验证

- [ ]  三处文档回写完成
- [ ]  本页状态已切
- [ ]  PR 合 main
- [ ]  commit：`docs(m9-patch1): record implementation discipline and close milestone`

---

## 12 · 与契约对齐表（验收口径）

| 条款 | 满足方式 |
| --- | --- |
| §5.3 不 cancel 原则 | 段一为独立 bg task，不被 cancel（除 shutdown），LangGraph + LLM 跑到自然结束 |
| §5.3 客户端断连 yield noop | 段二静默退出；段一持续运行、commit②、release lock |
| §6.4 (true, human/empty) → Waiting | 段一仍在跑 → `in_progress=true` → 前端轮询 |
| §6.4 (false, ai) → OK2 | 段一跑完 release lock → `in_progress=false` → 整段渲染 |
| §6.4 (false, human) → A4Late | 段一异常分支显式 rollback、不写 ai 占位行；释放 lock；前端轮询命中常规分支 |
| §3.3 单 worker + in-process running_streams | `running_streams` dict 仍在进程内，stop 接口语义不变 |
| §3.3 监控信号 | bg task 数 = `len(rr._chat_tasks)`，监控点天然 |
| §4.3 in_progress = EXISTS chat:lock | 段一 finally release lock 后翻转，时序保证准确 |

---

## 13 · 关键决策点替代方案对比（保留供回溯）

### 13.1 为什么 B+ 而不是 …

| 候选 | 为什么否决 |
| --- | --- |
| `asyncio.shield` 局部保护 | 治标不治本；shield 只保特定 await，无法保 yield 失败后续整个 finally 链；CancelledError 是 task 级、不可逆 |
| ARQ worker | 与 §3.3 单 worker + in-process `running_streams` stop 信号冲突；over-engineering |
| `asyncio.Queue` 无上限 | 弱网用户业务完成后段二仍硬撑 HTTP connection 推 frames（30s+），打穿 §3.3 200 并发门槛 |
| 只加 try/except 不解耦 | 已证伪：CancelledError 是 task 级、不可逆，所有 await 重新抛 |

### 13.2 为什么不在段二（后端→前端方向）做降速

段一全速 + 段二 sleep → queue 必然增长 → 必触发 `state.overflow` 断流 → 降速钩子立即失效。要让段二降速生效必须禁用断流机制——但断流是 B+ 的核心防御，二者不可兼得。单一钩子（段一）更干净。

### 13.3 为什么队满即断、不要水位线（HWM）

水位线模式服务于「多生产者 race」和「应对 burst」——我们单生产者顺序 put，两者皆无。队满即断用段一段二共享的 `state.overflow` flag：段一塞前 check 跳过 put、段二取前 check 立刻断流——单一变量贯穿。共享 flag 避开了「`queue.full()` 在 `await queue.get()` 之后检测永远是 False（size 已减 1）」的时序陷阱。

### 13.4 为什么前端必须识别 `flow_pause`

虽然不识别也能走自然 timeout，但 10s Waiting 在用户感知层面明显卡顿；本 patch 与 FE 清单页同节奏交付，识别成本极低（sseClient 类型联合加 1 变体 + chat.ts onEvent 加 1 case，复用既有 backgroundClose 通道），收益直接。

---

## 14 · 发现与建议（审查回写）

### 14.1 已收口写入计划主体（执行时一并处理）

- **`chat_stream` 顶层兜底过窄**：核验 main 后确认现状仅 `except HTTPException`，非 HTTPException（DB / Redis / OOM）在 commit① 与 StreamingResponse 之间抛出会绕过 `release_session_lock`，是孤儿 human bug 之外**另一条 lock 残留路径**。本 patch 扩展为捕获任意异常 → 已写入 §3 改动清单 [me.py](http://me.py) 行 / §7.2 改动列表 / §7.3 现状对照。
- **helper 不存在需先抽取**：`_commit_ai_row` / compression / thinking 状态机 / stream_graph_to_sse 包装 / stop 双分支等在 main 上是 generator 内 inline 实现，本 patch 必须「先抽 helper 再迁段一」 → 已写入 §3 与 §7.3。
- **`_MainGraphCompat` 删除影响面**：grep `app.api.me._main_graph` 找出所有 patch 引用点，统一改为 `rr.main_graph` → 已写入 §3。
- **commit① 与 create_task 之间异常路径缺测试**：补 G/W/T 用例 #8 → 已写入 §9.3。
- **段一 finally 中 `release_session_lock` 失败的可观测性**：原 `except Exception: pass` 静默吃异常、仅靠 180s TTL 兜底；改为 `logger.warning("release lock failed, rely on TTL", exc_info=True, extra={"sid": str(sid)})` 留痕 → 已写入 §3 改动清单 [me.py](http://me.py) 行 / §7.2 改动列表 / §7.4 骨架 finally 处。

### 14.2 剥离为独立 issue（不在本 patch）

- **日轮次硬控**（§0.2 已声明不做）：写入 [](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21) 单独追踪，§11.2 收尾步骤负责创建条目。

## 附录 A · 讨论沿革（供回溯，不用于执行）

本计划在 2026-05-28 至 2026-05-29 与项目负责人的技术讨论中迭代而成。关键收敛点：

1. **生命周期解耦**是修 bug 的核心，三个机制（解耦 / 断流 / 降速）正交
2. **降速钩子放段一**（控生产），不放段二（避免与断流互斥）
3. **队满即断**比水位线更简洁，单生产者场景无需水位线
4. **队满信号用段一段二共享的 `state.overflow` flag**：第一次 QueueFull 翻 flag → 段一塞前跳 put、段二取前断流。避开了「`queue.full()` 在 get 之后检测永远 False」的时序陷阱
5. **强杀 / 弱网 / 正常**三场景下段一代码路径完全一致——设计真正优雅之处
6. **段一异常路径**走显式 `db.rollback()` + 不写 ai 占位行（§6.4 (false, human) → A4Late，无需为异常加特殊前端路径）
7. **`running_streams` 注册时机**必须在 `create_task` 之前，避免段一进循环时 race
8. **`_chat_tasks` 必须用 `default_factory`**，class-level `= {}` 是 Python mutable default 大坑

---

## 附录 B · 三场景时序对照（设计自检）

```
A) 强杀（TCP RST）
   段一 ───────────────────── commit② ── release lock ── done
   段二 ─x（ConnectionError 静默退出）
   queue   段一 put_nowait，约 2.5s 内满 → state.overflow=True、之后跳过 put

B) 弱网（TCP 慢但活）
   段一 ───────────────────── commit② ── release lock ── done
   段二 ─取─yield─[state.overflow]─yield flow_pause─return
   queue   渐渐堆到 128 → 段一 QueueFull → state.overflow=True

C) 强网（正常）
   段一 ─put─put─put────── commit② ── release lock ── done
   段二 ─取─yield─取─yield────────── 收到 None ── 退出
   queue   始终 0-5 帧
```

三个场景下：

- **段一行为完全一致** — commit② 必落、lock 必释放
- **DB 末态一致** — `(in_progress=false, last_role=ai)`（异常路径除外，按 §6.4 (false, human) → A4Late）
- **前端最终结局一致** — 实时流（C）或 Resume 整段渲染（A/B）或 A4Late（异常）