# Refactor: M9-patch0 · 图边界 + Runtime DI 校准

<aside>
📋

**类型**：重构（无业务行为变化） · **PR 边界**：独立 PR，先于 M9 主体合入

**分支**：`refactor/m9-patch0-graph-boundary-di`

**父计划**：[M9 · 三级干预集成 — 前置讨论草案](https://www.notion.so/M9-4088f037ad954d37bd1975ddc32f1842?pvs=21)

**范式基线**：[§十四 · 三方范式整合 · FastAPI + ARQ + LangGraph](https://www.notion.so/FastAPI-ARQ-LangGraph-31aeae0f24e54d7396172de2ecbabf60?pvs=21)

**偏差记录**：本页子页

**依赖**：M8 已合 main（alembic head `412aed826359`）；langgraph ≥ 1.2（Runtime API）

</aside>

## §0 目标与边界

把 M8 期遗留的「图边界 + Runtime DI」范式偏差**一次性归零**，让 M9 主体 PR 只承载三级干预业务逻辑。

**做的事**（T0–T16，§十四 §六 8 处不合规全部归零）：

- 建立 `RuntimeResources` 进程级资源容器（T0）
- 引入 `ChatContextSchema` / `AuditContextSchema`（T1 / T1b）
- 净化 `MainDialogueState`（T2）
- chat_graph 工厂化 + 拓扑改写 + 3 个消息装配节点（T3 / T4），**删 `main_graph` 模块级别名**
- 4 个 chat 节点改 Runtime DI（T5 / T6 / T7）
- API 层改 `app.state.resources`（T8）
- FastAPI lifespan + ARQ `on_startup` 对偶（T9 / T10）
- audit_graph 工厂无参 + 6 节点 DI + text SQL → ORM（T11 / T12）
- 死代码 sweep（T13）
- 三组回归测试（T14 / T15 / T16）

**不做的事**（明确划入 M9 主体）：

- `load_messages_by_role` 物理查询原语 → M9 主体 **C 层**
- `send_intervention_notification` stub → M9 主体 **F 层**
- `crisis_locked_message_id` / `intervention_type_planned` / `audit_anchor_window` / `audit_redline_context` 等业务字段 → M9 主体 **B 层**
- LangGraph streaming v2 / checkpointer → M11 / M14
- `notify/` 目录骨架已存在 M3 占位，patch0 不动

**完成定义**：M8 既有测试 + T14/T15/T16 新增测试全绿；M9 主体 PR 可基于 patch0 的 main 顺序 cherry-pick 而无需 rebase 冲突。

## §1 §十四 §六 8 处不合规 → T# 映射

| # | §十四 §六 不合规点 | 归零任务 |
| --- | --- | --- |
| 1 | `main_graph` 模块加载期 compile（全局单例） | **T3**（工厂化 + 删别名） |
| 2 | `load_audit_state` 节点内读 `settings.*` | **T5** |
| 3 | `call_main_llm` 节点内 `get_chat_llm()` lru_cache 调用 | **T6** |
| 4 | 消息装配 `_assemble_llm_messages`  • `inject_guidance` 跨节点 staging | **T4 + T6**（装配移入图节点 `build_messages_main` 一次性消费 guidance；crisis / redline 占位委派 main） |
| 5 | `MainDialogueState` 含 5 个非 LangGraph 范式字段（`pending_guidance` 等） | **T2** |
| 6 | audit_graph `build_audit_graph(settings=...)` closure 注入 | **T11**（改无参工厂 + 节点 Runtime DI） |
| 7 | `api/me.py` 直接 import `main_graph`  • 内联拼 `initial_state` | **T8** |
| 8 | audit `_load_messages_from_pg` 使用 `text()` 字符串 SQL | **T12** |

## §2 执行前现状核验（5 分钟，跑前必做）

执行 agent 第一步跑下列断言，结果记入偏差记录页 §0。任一不符 → 暂停回讨论。

- [ ]  `alembic heads` 输出仅 `412aed826359 (head)`
- [ ]  `python -c "import langgraph; print(langgraph.__version__)"` ≥ `1.2.0`（Runtime API 入口）
- [ ]  `grep -rn "^main_graph = " backend/app/chat/graph.py` 命中模块级单例（**待删目标**）
- [ ]  `grep -rn "@lru_cache" backend/app/chat/factory.py backend/app/auth/redis_client.py` 命中 `get_chat_llm` / `get_audit_redis`（**待删目标**）
- [ ]  `grep -rn "_assemble_llm_messages\|inject_guidance" backend/app/chat/` 命中函数定义与调用（**待删目标**）
- [ ]  `grep -rn "pending_guidance" backend/` 仅在 `chat/state.py` 定义 + `chat/graph.py` 写读 ≤ 5 处（**待删字段**，无 audit-side 引用即可一次性删）
- [ ]  `grep -rn "from sqlalchemy import text\|\\btext(" backend/app/audit/graph.py` 命中（**待改 ORM**）
- [ ]  `grep -rn "settings\\." backend/app/chat/graph.py backend/app/audit/graph.py` 命中且**仅在节点函数体内**（**待改 Runtime DI**）

## §3 执行步骤 T0–T16（按 9 层 Step 顺序）

### Step 0 — 建分支

```bash
git checkout -b refactor/m9-patch0-graph-boundary-di main
```

### Step L0 — 进程级资源容器

**T0**：新建 `backend/app/runtime.py`：

```python
from dataclasses import dataclass
from typing import TYPE_CHECKING

from arq.connections import ArqRedis
from langgraph.graph.state import CompiledStateGraph
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, AsyncSession

if TYPE_CHECKING:
    from app.config import Settings

@dataclass(frozen=True)
class RuntimeResources:
    settings: "Settings"
    db_engine: AsyncEngine
    db_session_factory: async_sessionmaker[AsyncSession]
    audit_redis: Redis
    arq_pool: ArqRedis
    main_graph: CompiledStateGraph
    audit_graph: CompiledStateGraph

async def build_runtime(settings: "Settings") -> RuntimeResources:
    """构建进程级资源容器；FastAPI lifespan / ARQ on_startup 共用。"""
    # 顺序：db_engine → session_factory → audit_redis → arq_pool → main_graph → audit_graph
    ...

async def teardown_runtime(rr: RuntimeResources) -> None:
    """对偶关闭：arq_pool → audit_redis → db_engine。图无需关闭。"""
    ...
```

**注意**：`@dataclass(frozen=True)`；`build_runtime` 内**只构图、不预热 LLM**（LLM 由节点首次调用时按 Runtime 取 settings 创建）。

### Step L1 — ContextSchema 定义

**T1**：新建 `backend/app/chat/context_schema.py`：

```python
import uuid
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.config import Settings

@dataclass(frozen=True)
class ChatContextSchema:
    session_id: uuid.UUID
    child_user_id: uuid.UUID
    child_profile: dict[str, Any]
    age: int
    gender: str
    user_input: str
    settings: Settings
    db_session_factory: async_sessionmaker[AsyncSession]
    audit_redis: Redis
```

**T1b**：新建 `backend/app/audit/context_schema.py`：

```python
@dataclass(frozen=True)
class AuditContextSchema:
    session_id: uuid.UUID
    child_user_id: uuid.UUID
    target_message_id: uuid.UUID
    max_iter: int
    settings: Settings
    db_session_factory: async_sessionmaker[AsyncSession]
    audit_redis: Redis
```

### Step L2 — 净化 MainDialogueState

**T2**：`backend/app/chat/state.py` 删除以下字段（M8 期非范式 staging）：

- `pending_guidance` —— 跨节点 staging，T4 后由图节点内变量替代
- `assembled_messages` —— 同上
- `child_profile` / `age` / `gender` / `user_input` —— 移入 `ChatContextSchema`（运行时不可变上下文，不应在 State）

净化后 `MainDialogueState`：

```python
class MainDialogueState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    audit_state: Literal["safe", "crisis", "redline", "guidance"] | None
    turn_number: int
    generated_token_count: int
    client_alive: bool
    user_stop_requested: bool
```

**对齐**：§十四 §六 #5。

### Step L3 — chat_graph 工厂化 + 装配入图

**T3**：`backend/app/chat/graph.py` 末尾改写：

```python
def build_main_graph() -> CompiledStateGraph:
    builder = StateGraph(MainDialogueState, context_schema=ChatContextSchema)
    builder.add_node("load_audit_state", load_audit_state)
    # T4：3 个消息装配节点（patch0 期 crisis / redline 委派 main 等价逻辑）
    builder.add_node("build_messages_main", build_messages_main)
    builder.add_node("build_messages_crisis", build_messages_crisis)
    builder.add_node("build_messages_redline", build_messages_redline)
    builder.add_node("call_main_llm", call_main_llm)
    builder.add_node("call_crisis_llm", call_crisis_llm)
    builder.add_node("call_redline_llm", call_redline_llm)
    builder.set_entry_point("load_audit_state")
    # 拓扑铁律（§十四 §五 5.2）：route 先分流到 build_messages_*，再各自进 LLM
    # 最终拓扑；M9 主体 PR 仅改函数体，拓扑零 diff
    builder.add_conditional_edges("load_audit_state", route_by_risk, {
        "safe": "build_messages_main",
        "guidance": "build_messages_main",
        "crisis": "build_messages_crisis",
        "redline": "build_messages_redline",
    })
    builder.add_edge("build_messages_main", "call_main_llm")
    builder.add_edge("build_messages_crisis", "call_crisis_llm")
    builder.add_edge("build_messages_redline", "call_redline_llm")
    builder.add_edge("call_main_llm", END)
    builder.add_edge("call_crisis_llm", END)
    builder.add_edge("call_redline_llm", END)
    return builder.compile()
```

**严格删除**（§十四 §六 #1）：

```python
# 整段删除，不留向后兼容别名
# main_graph = _builder.compile()
```

**T4**：新增 3 个消息装配节点（替代原 `_assemble_llm_messages` + `inject_guidance` 两段；**patch0 期 crisis / redline 节点函数体委派 main 等价逻辑，M9 主体 PR 仅改函数体，拓扑零 diff**）：

```python
async def build_messages_main(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    ctx = runtime.context
    # 1. 从 ctx.db_session_factory 取历史 messages（active）
    # 2. 拼 SystemMessage（含 child_profile / age / gender，从 ctx 取，不再读 state）
    # 3. 若 state["audit_state"] == "guidance"：在末位 HumanMessage 之前插入
    #    SystemMessage(guidance_text)，一次性消费
    #    —— 替代 M8 期 pending_guidance + inject_guidance + _assemble_llm_messages 跨节点透传
    # 4. 追加 HumanMessage(ctx.user_input)
    # 5. 返回 {"messages": [...]}（由 add_messages reducer 合入）
    ...

async def build_messages_crisis(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    # patch0 期：委派 build_messages_main 等价逻辑（行为零变化）
    # M9 主体 D 层：替换为 crisis-specific 装配（锁定历史窗口 + 危机专用 SystemMessage）
    return await build_messages_main(state, runtime)

async def build_messages_redline(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    # patch0 期：委派 build_messages_main 等价逻辑（行为零变化）
    # M9 主体 D 层：替换为 redline-specific 装配（注入 redline 上下文 + 红线专用 SystemMessage）
    return await build_messages_main(state, runtime)
```

### Step L4 — chat 节点 Runtime DI

**T5**：`load_audit_state` 改 Runtime DI（§十四 §六 #2）：

```python
async def load_audit_state(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    ctx = runtime.context
    redis = ctx.audit_redis
    ttl = ctx.settings.audit_redis_ttl_seconds
    timeout = ctx.settings.audit_wait_timeout_seconds
    # ... 原逻辑改用上面 3 个变量
```

**T6**：`call_main_llm` 改 Runtime DI（§十四 §六 #3 / #4）：

```python
async def call_main_llm(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    llm = ChatDeepSeek(
        model=runtime.context.settings.deepseek_chat_model,
        api_key=runtime.context.settings.deepseek_api_key,
        ...
    )
    # 直接消费 state["messages"]（已由 build_messages_main 装配）
    ...
```

**同步删**（T13 完整列表，此处前置）：`get_chat_llm` / `_assemble_llm_messages` / `inject_guidance` 节点。

**T7**：`call_crisis_llm` / `call_redline_llm` stub 签名同步改 `Runtime[ChatContextSchema]`：

```python
async def call_crisis_llm(state, runtime: Runtime[ChatContextSchema]) -> dict:
    # M9 主体 D 层实现；patch0 落回 call_main_llm 等价行为
    ...
```

### Step L5 — API 层接入

**T8**：`backend/app/api/me.py` 改写（§十四 §六 #7）：

```python
# 删除：from app.chat.graph import main_graph
# 删除：initial_state 内联字典中 child_profile / age / gender / user_input 字段

@router.post("/me/chat/stream")
async def chat_stream(request: Request, ...):
    rr: RuntimeResources = request.app.state.resources
    ctx = ChatContextSchema(
        session_id=...,
        child_user_id=...,
        child_profile=...,
        age=...,
        gender=...,
        user_input=...,
        settings=rr.settings,
        db_session_factory=rr.db_session_factory,
        audit_redis=rr.audit_redis,
    )
    initial_state: MainDialogueState = {
        "messages": [],
        "audit_state": None,
        "turn_number": ...,
        "generated_token_count": 0,
        "client_alive": True,
        "user_stop_requested": False,
    }
    async for event in rr.main_graph.astream(
        initial_state,
        context=ctx,
        stream_mode="messages",
    ):
        ...
```

### Step L6 — lifespan + ARQ on_startup 对偶

**T9**：`backend/app/main.py` 改 FastAPI lifespan：

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    rr = await build_runtime(settings)
    app.state.resources = rr
    try:
        yield
    finally:
        await teardown_runtime(rr)

app = FastAPI(lifespan=lifespan)
```

**T10**：`backend/app/audit/worker.py` 改 ARQ on_startup（与 lifespan 对偶）：

```python
async def on_startup(ctx: dict) -> None:
    settings = get_settings()
    rr = await build_runtime(settings)
    ctx["resources"] = rr

async def on_shutdown(ctx: dict) -> None:
    rr: RuntimeResources = ctx["resources"]
    await teardown_runtime(rr)

class WorkerSettings:
    functions = [run_audit]
    on_startup = on_startup
    on_shutdown = on_shutdown
```

`run_audit` 从 `ctx["resources"]` 取 `rr.audit_graph` 与 `rr.db_session_factory`，构造 `AuditContextSchema` 调 `rr.audit_graph.astream(..., context=audit_ctx)`。

### Step L7 — audit_graph 改造

**T11**：`backend/app/audit/graph.py`（§十四 §六 #6）：

- `build_audit_graph(settings=...)` → `build_audit_graph()` 无参
- 6 个节点（`load_context` / `_load_messages_from_pg` / `call_audit_llm` / `route_by_decision` / `write_results` / `enqueue_followup`）签名改 `(state, runtime: Runtime[AuditContextSchema]) -> dict`
- 节点内从 `runtime.context.settings` / `runtime.context.db_session_factory` / `runtime.context.audit_redis` 取资源
- 模块末尾删除 `audit_graph = build_audit_graph(...)` 模块级单例，由 `runtime.build_runtime` 调一次

**T12**：`_load_messages_from_pg` 节点从 `text()` 改 ORM（§十四 §六 #8）：

```python
# 旧：result = await session.execute(text("SELECT id, role, content, created_at FROM messages WHERE ..."), {...})

# 新：
from sqlalchemy import select
stmt = (
    select(Message)
    .where(Message.session_id == ctx.session_id)
    .where(Message.status == "active")
    .order_by(Message.created_at.asc(), Message.id.asc())
)
result = await session.execute(stmt)
messages = result.scalars().all()
```

### Step L8 — 死代码 sweep

**T13** 一次性删除（删完跑全测套件验证）：

- `app/chat/factory.py::get_chat_llm`（lru_cache 工厂，文件若空则删）
- `app/auth/redis_client.py::get_audit_redis`（lru_cache 单例；调用方改 `rr.audit_redis`）
- `app/audit/audit_tools.py`（如存在的旧 tools 模块，M8 期占位未启用）
- `app/chat/graph.py::_assemble_llm_messages`
- `app/chat/graph.py::inject_guidance`
- `MainDialogueState.pending_guidance`（T2 删字段，T13 grep 复查残余写读）
- `api/me.py::initial_state` 内联 child_profile / age / gender / user_input 字段（T8 已替代）
- `_session_maker` 私有名（改 `db_session_factory` 经 `rr` 注入，不再模块级 import）

**grep 收尾断言**（写入偏差页 §1）：

- `grep -rn "get_chat_llm\|get_audit_redis\|audit_tools\|_assemble_llm_messages\|inject_guidance\|pending_guidance" backend/app/` 全部零结果
- `grep -rn "^main_graph\s*=" backend/app/` 零结果
- `grep -rn "\\btext(" backend/app/audit/graph.py` 零结果

### Step L9 — 测试

**T14**：`tests/test_runtime.py`：

1. `test_build_runtime_returns_frozen` —— 赋值抛 `FrozenInstanceError`
2. `test_build_runtime_graphs_compiled` —— `rr.main_graph` / `rr.audit_graph` 有 `astream`
3. `test_teardown_runtime_order` —— mock 三个 close，断言顺序 arq → redis → db

**T15**：`tests/chat/`：

- `test_graph_factory.py`：`build_main_graph` 返回 compiled、节点集 = {load_audit_state, build_messages_main, build_messages_crisis, build_messages_redline, call_main_llm, call_crisis_llm, call_redline_llm}（**7 节点**，不含 inject_guidance）、`route_by_risk` 四分支（safe / guidance / crisis / redline）
- `test_build_messages_main.py`：mock `ChatContextSchema` + seed messages → 装配产物含 SystemMessage + 历史 + 新 HumanMessage；`audit_state="guidance"` 时末位 HumanMessage 之前含 guidance SystemMessage
- `test_build_messages_crisis_redline_delegate.py`：patch0 期 `build_messages_crisis` / `build_messages_redline` 返回与 `build_messages_main` 等价（行为零变化硬断言）
- `test_normal_path_regression.py`：走 `api_client` SSE，断言 patch0 后 normal 路径 `session_meta → delta → end` 事件序列与 M8 一致（业务无变化硬断言）
- `test_no_main_graph_alias.py`：反向断言 `from app.chat.graph import main_graph` 抛 `ImportError`

**T16**：`tests/audit/`：

- `test_audit_graph_factory.py`：`build_audit_graph()` 无参可调
- `test_audit_node_di.py`：mock `Runtime[AuditContextSchema]` → 节点能从 `runtime.context` 取资源
- `test_load_messages_from_pg_orm.py`：seed 10 条消息（含 compressed），断言 ORM 路径仅返回 active 且时间正序
- `test_no_text_sql_in_audit.py`：静态 grep `text(` 在 `app/audit/graph.py` 零结果

**隔离纪律**：严格按 [M6-patch · 测试隔离纪律加固](https://www.notion.so/M6-patch-0636f26e98f94916858983c30fdad01d?pvs=21)：`db_session` savepoint / `api_client` ASGI / `redis_client` fakeredis / 禁 subprocess / 禁 flushdb / 禁自建 engine。

## §4 提交序列（Conventional Commits）

按下列 11 步推进，每个独立 commit：

1. `feat(runtime): add RuntimeResources frozen dataclass + build/teardown`（T0）
2. `feat(chat): add ChatContextSchema`（T1）
3. `feat(audit): add AuditContextSchema`（T1b）
4. `refactor(chat): purge non-paradigm fields from MainDialogueState`（T2）
5. `refactor(chat): factory build_main_graph + 3 build_messages_* nodes (main + crisis/redline placeholders), drop module-level main_graph alias`（T3 + T4）
6. `refactor(chat): runtime DI for load_audit_state / call_main_llm / crisis / redline stubs`（T5 + T6 + T7）
7. `refactor(api): wire app.state.resources + ChatContextSchema in chat_stream`（T8）
8. `refactor(runtime): FastAPI lifespan + ARQ on_startup/on_shutdown dual`（T9 + T10）
9. `refactor(audit): no-arg factory + node runtime DI + text SQL → ORM select`（T11 + T12）
10. `chore(cleanup): remove get_chat_llm / get_audit_redis / audit_tools / _assemble_llm_messages / inject_guidance / pending_guidance`（T13）
11. `test(runtime,chat,audit): patch0 regression suite`（T14 + T15 + T16）

**合并方式**：PR 标题 `refactor: M9-patch0 · 图边界 + Runtime DI 校准`，squash merge 到 main。

## §5 验收清单

- [ ]  alembic 无新增 revision（patch0 不动 schema）
- [ ]  `basedpyright backend/app/` 全绿（standard 模式）
- [ ]  `ruff check backend/` 全绿
- [ ]  M8 既有测试套件全绿（不允许 skip）
- [ ]  T14 / T15 / T16 新增测试全绿
- [ ]  **`main_graph` 模块级别名清除断言**：`grep -rn "^main_graph\\s*=" backend/app/` 零结果
- [ ]  **audit `text()` SQL 清除断言**：`grep -rn "\\btext(" backend/app/audit/graph.py` 零结果
- [ ]  **拓扑铁律验收（§十四 §五 5.2）**：`build_main_graph()` 节点集 = {load_audit_state, build_messages_main, build_messages_crisis, build_messages_redline, call_main_llm, call_crisis_llm, call_redline_llm}（**7 节点**），`route_by_risk` 四分支（safe / guidance / crisis / redline）
- [ ]  **`add_messages` reducer 多分支累积验收**：同一 turn 走 main / crisis / redline 任一分支，`state["messages"]` 末态仅含本次装配产物 + 原历史，无跨分支重复 / 残留
- [ ]  **§十四 §六 8 处不合规归零硬断言**：T13 grep 收尾断言全部满足
- [ ]  §1 范式基线对照表 + §2 现状核验结果已写入偏差记录子页
- [ ]  commit 序列符合 §4
- [ ]  Gate A（自审）+ Gate B（与本计划对照）勾过

## §6 风险与回滚

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| T3 删 `main_graph` 别名后有遗漏调用方 | 启动期 ImportError | T8 同 commit 改 `api/me.py`；T13 grep 收尾断言；PR 跑 full smoke |
| T2 删 `pending_guidance` 字段后 audit-side 残留引用 | 跨服务运行时 KeyError | §2 前置核验 grep `pending_guidance` 限 chat-side ≤ 5 处；不符暂停 |
| T4 装配入图后历史消息顺序变化 | LLM 行为漂移 | `test_normal_path_regression` 断言 SSE 事件序列与 M8 一致；可选加 fixture 录制 LLM 输入对比 |
| T6 节点内每次 new ChatDeepSeek 性能 | 每轮多一次连接开销 | 实测；若 P99 +>50ms 再引入 Runtime 内 LLM 缓存（后续 patch，不进 patch0） |
| T9 / T10 资源构建顺序错位 | 启动期 dependency 异常 | `build_runtime` 内显式顺序：db_engine → session_factory → audit_redis → arq_pool → main_graph → audit_graph；T14 测试覆盖 teardown 反向顺序 |
| T11 audit 节点 DI 与 M8 测试不兼容 | 测试夹具大批量改 | 提供 `make_audit_runtime(...)` test helper；T16 内统一使用 |
| T12 ORM 查询 N+1 / 性能回退 | audit 任务 P99 升 | `select(Message)` 单查询无 N+1；本地 EXPLAIN 对比 M8 期 `text()` 路径执行计划 |

**回滚策略**：本 patch 单一 PR，`git revert` 即可；M8 业务功能保持原样。

## §7 与 M9 主体的交接

M9 主体 PR（草案 §阶段 2）分 A–G 层，patch0 落地后 cherry-pick 不冲突的路线：

- **A 层（schema）**：M9 主体加 `crisis_locked_message_id` 等业务字段到 `AuditContextSchema` / `MainDialogueState`，patch0 已留 Context / State 骨架，diff 只在字段定义行
- **B 层（state 字段消费）**：`intervention_type_planned` / `audit_anchor_window` / `audit_redline_context` 由 M9 主体 B 层一次加入并消费，patch0 不预留
- **C 层（物理查询）**：`load_messages_by_role` 由 M9 主体 C 层引入到 `app/chat/context.py`，patch0 不前置
- **D 层（节点真接入）**：`build_messages_crisis` / `build_messages_redline` + `call_crisis_llm` / `call_redline_llm` 真业务实现；patch0 已把节点全部就位、签名 Runtime DI、**拓扑锁定为 M9 主体最终态**；主体 PR **diff 仅在函数体，拓扑零 diff**
- **E 层（enqueue_audit 透传）**：`ai_msg_id` 透传，patch0 不动 `enqueue_audit` 签名
- **F 层（通知）**：`send_intervention_notification` stub 由 M9 主体 F 层引入到 `app/notify/intervention.py`，patch0 不前置
- **G 层（集成测试）**：M9 主体 G 层加端到端三级干预测试，patch0 T14 / T15 / T16 已覆盖范式回归

[M9-patch0 · 执行偏差记录](https://www.notion.so/M9-patch0-35b24688f16645f999a931bed37aa745?pvs=21)