# M6 · 主对话链路 - 后端核心 — 实施计划 (6/17)

<aside>
🔌

**M6 · 主对话链路 - 后端核心 — 实施计划**

本页是 M6 里程碑的执行计划。设计基线见 [M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21)；阶段 1 / 阶段 2 决策已全部对齐，本页只负责把决策转成可执行的步骤序列。执行纪律遵循 [Step-Execute Skill v1.4 更新稿](https://www.notion.so/Step-Execute-Skill-v1-4-a92066d4fc6f43a8b3cc177c55c1d560?pvs=21)：每步独立 commit、独立验证、独立审核轮次。

</aside>

## 一、目标概述

实现子端主对话链路的**后端核心**：5 个 API 端点 + LangGraph 主对话图（5 核心 + 3 stub 节点）+ sessions / messages 持久化 + 双锁 + SSE 7 事件 + 末行决策矩阵 末行检查 + 不 cancel 客户端断连不 cancel。

### 1.1 做什么

- **数据模型**：`messages` 表加 `status`（PG ENUM `messagestatus`，值 `active` / `discarded`）/ `finish_reason`（String）两列；索引改 partial WHERE status='active'
- **`chat/` 模块拆分**：7 文件（state / graph / context / prompts / locks / sse / dashscope_chat）；其中 graph / sse / dashscope_chat 在 **Step 0** 完成 plain-class 重构 + dev_chat 兼容入口保留，state / context / prompts / locks 在 Step 2 起新建
- **LangGraph 图**：5 节点（load_audit_state / call_main_llm / call_crisis_llm / call_redline_llm / inject_guidance）+ 1 conditional router（route_by_risk）；持久化与 enqueue_audit 不进图，由 [me.py](http://me.py) generator 调用 helper（**T5 唯一写入点**）；stub 节点内部走主 LLM 兜底 + 显著 logger.warning
- **5 端点**：`POST /me/chat/stream` · `POST /me/sessions/{id}/stop` · `GET /me/sessions` · `GET /me/sessions/{id}/messages` · `DELETE /me/sessions/{id}`
- **双锁** + Lua DEL nonce 防误删
- **SSE 7 事件**：`session_meta` / `thinking_start` / `thinking_end` / `delta` / `end` / `stopped` / `error`
- **末行决策矩阵** 末行检查 + 孤儿 human 自动 `status='discarded'` 自愈
- **不 cancel 原则**：客户端 TCP 断连不 cancel LLM 流，yield 改 noop
- **title M6 生成**：`user_content` 前 12 字符（按 grapheme 边界）
- **每步随附单测（test-as-you-go）+ mock LLM 集成测**

### 1.2 不做什么（明确划界，防止范围蔓延）

- ❌ 审查 worker / `rolling_summaries` 写入 → **M8**
- ❌ 真实三级介入 LLM（crisis / redline）→ **M9**，M6 stub 走主 LLM 兜底
- ❌ 前端 UI / 状态机 / 短轮询 → **M7**
- ❌ 多 worker 部署 / Redis Pub/Sub stop 广播 / sticky routing → **撞容量边界后改造**，M6 不预先做
- ❌ 后台清理孤儿任务 → **M11+**
- ❌ title LLM 自动改写 → **M8** 配套
- ❌ `enqueue_audit` 真实 ARQ 接入 → **M8**，M6 仅 no-op + logger.warning
- ❌ 真 LLM 调用测试（cost + flaky）→ 永远不做
- ❌ `dev_chat.py` 物理删除 → **M7**，M6 仅加 DEPRECATED banner

## 二、前置条件

- `main` 分支干净，`alembic current` 输出 `3522d5e7ba69` (M4.8 baseline + token indexes hotfix)
- backend 容器：PG / Redis 可用；`pytest backend/tests` 全绿
- DashScope API key 在本地 `.env` 已配置（仅用于手工联调，单测 / 集成测都不依赖真 API）
- 设计基线 [M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21) 已是最新版本（含 D3 / D6 / D7 修订）
- M5 前端账号体系不阻塞本计划（本计划仅后端 + dev hub 调试入口）

## 三、执行步骤

### Step 0 · LLM 客户端 + Graph + SSE plain-class 化（前置基础重构）

> ⚠️ **基础重构必须前置 + 一次性级联**。原因：现网 M3 实装 `ChatDashScopeQwen` 是 `BaseChatModel` 子类，[graph.py](http://graph.py) 走 `ainvoke` + 外层 `astream_events()` + `on_chat_model_stream` 的流式通路；一旦改为 plain class，LangGraph 不再 emit `on_chat_model_stream`，graph / sse / dev_chat 必须同步切换。dev_chat 在 M7 才删除，本步必须保持其工作不退化。
> 

**任务**

- [ ]  `dashscope_chat.py`：实现 `DashScopeCallOptions`（Pydantic）+ `ChatDashScopeQwen` Plain Class（**不**继承 BaseChatModel）；按架构基线 §十（2026-04-30 决议）落地；字段 / 方法签名见原 Step 5 描述（保留 thinking 分流：reasoning_content → `additional_kwargs`，content → `content`；**末帧 finish_reason 透传**：DashScope SDK `choice.finish_reason` 命中白名单 `stop` / `length` / `content_filter` 时，写入末 chunk 的 `response_metadata={"finish_reason": ...}`，供 Step 8b 末段事务消费）
- [ ]  删除 `chat/llm.py`：`get_chat_llm()` 单例移入 `dashscope_chat.py` 顶部
- [ ]  `graph.py` 临时单节点版：保留 M3 形态的单节点 `call_main_llm`，但**节点内部**改为 `async for chunk in llm.astream(state["messages"]): writer(chunk)`（用 LangGraph custom streaming API）；**不**写 ai 行 / 不更新 last_active_at（持久化收敛到 generator，见 Step 6 / 8b）；图边仍 `START → call_main_llm → END`
- [ ]  `sse.py`（**双 framer 并存，禁止合并**）：
    - **保留** `_sse_pack(event_type, **payload)` 函数原文不动（M3 单行协议 `data: {"type": ..., ...}\n\n`），供 dev_chat 兼容路径使用
    - **新增** `_frame_sse_event(event_type, data: dict) -> bytes`（M6 多行协议 `event: <type>\ndata: <json>\n\n`），供 me 主路径使用
    - 新增 `stream_to_sse(graph_stream)` 主路径：消费 `AIMessageChunk` 流，使用 `_frame_sse_event` emit `thinking_start` / `thinking_end` / `delta`；供 Step 8b 调用
    - **保留** `stream_chat(message: str, sid: str)` 兼容入口：内部继续用 `_sse_pack` 拼装 M3 协议帧（`start` / `delta` / `error` / `end`），但流来源改为 `main_graph.astream(..., stream_mode="custom")` 的 `AIMessageChunk` 流（不再读 `astream_events` 的 `on_chat_model_stream`），让 dev_chat 完全不感知重构
- [ ]  `dev_chat.py`：**不动业务逻辑**，仅在文件顶部加 DEPRECATED docstring + 模块加载时 `logger.warning("dev_chat is DEPRECATED, will be removed in M7")`（提前标记，原 Step 9 不再重复加 banner）
- [ ]  单测：
    - `tests/chat/test_dashscope_chat.py`：DashScopeCallOptions 默认值 / `model_dump(exclude_none=True)` / `astream` 返回 AIMessageChunk 流 / reasoning-content 分流 / **末帧 finish_reason 透传**（mock SDK 返回 stop / length / content_filter 时末 chunk `response_metadata["finish_reason"]` 命中对应值；非白名单值不透传）/ `ainvoke` 收集完整响应 / SDK 非 200 → DashScopeAPIError
    - `tests/chat/test_sse.py`：`stream_to_sse` 帧顺序（thinking_start / thinking_end / delta）+ `stream_chat` 兼容路径帧格式不退化
    - `tests/api/test_dev_chat.py`：dev_chat 端到端回归（mock LLM）—— 落地后必须仍全绿

**验证清单**

- ✅ `ChatDashScopeQwen` 是 plain class，**不**继承 BaseChatModel
- ✅ `DashScopeCallOptions` 序列化正确（exclude_none / 嵌套 SearchOptions）
- ✅ `graph.astream(initial_state, stream_mode="custom")` 在临时单节点上跑通，writer 透出 AIMessageChunk
- ✅ `sse.stream_chat` 对外 SSE 帧格式不变（M3 单行 `data: {"type":..., ...}`，dev_chat 回归测全绿）
- ✅ `sse._sse_pack` 与 `sse._frame_sse_event` 双 framer 并存：前者供 dev_chat、后者供 me 主路径
- ✅ `AIMessageChunk` 末帧 `response_metadata["finish_reason"]` 透传 SDK 真实值（stop / length / content_filter 三类全覆盖）
- ✅ `dev_chat` 模块加载时 logger.warning 输出一次，路由仍可访问
- ❌ 不实现 5 章节 SystemMessage / build_context / 5 端点 / 双锁 / 末行决策矩阵 等业务逻辑（留 Step 1+）

**Commit**

```jsx
refactor(chat): replace BaseChatModel with plain class + custom-streaming graph

- ChatDashScopeQwen: drop BaseChatModel inheritance, expose astream/ainvoke
- DashScopeCallOptions: Pydantic model for SDK call params
- graph: single-node call_main_llm uses llm.astream + writer (custom stream)
- sse: stream_to_sse main path; stream_chat compat shim for dev_chat
- dev_chat: DEPRECATED docstring + load-time warning (M7 deletes file)
- delete chat/llm.py (merged into dashscope_chat.py)
- tests: dashscope_chat / sse / dev_chat regression all green
```

---

### Step 1 · 分支创建 + alembic 迁移 + ORM 同步 + 索引改造

**任务**

- [ ]  `git checkout main && git pull --rebase origin main`
- [ ]  `git checkout -b feat/m6-main-chat-backend`
- [ ]  确认 `alembic current` = `3522d5e7ba69 (head)`；`pytest backend/tests` 全绿
- [ ]  `alembic revision -m "m6 messages status finish_reason"` 新建 revision（不重建 baseline）
- [ ]  同步 `backend/app/models/enums.py`：新增 `MessageStatus(str, enum.Enum)`，值 `active` / `discarded`（与 `SessionStatus` 一致用 PG ENUM，不用 TEXT；项目其他 status 列均为 PG ENUM）
- [ ]  upgrade：先创建 `messagestatus` PG ENUM 类型 → `ALTER TABLE messages ADD COLUMN status messagestatus NOT NULL DEFAULT 'active'` → `ALTER TABLE messages ADD COLUMN finish_reason TEXT`（finish_reason 值域开放：stop / length / content_filter / user_stopped / ...，维持 String 不用 ENUM，避免 ALTER TYPE 增值）
- [ ]  downgrade：对应 DROP COLUMN + DROP TYPE messagestatus
- [ ]  同步 `backend/app/models/chat.py`（Message ORM）：增 `status: Mapped[MessageStatus]` / `finish_reason: Mapped[str | None]` 字段
- [ ]  **索引改造（同 revision 内 drop 旧 + create 新 partial）**：drop `idx_messages_session` / `idx_sessions_child`；create `idx_messages_session_active_created (session_id, created_at DESC, id DESC) WHERE status='active'` 与 `idx_sessions_child_active_lastactive (child_user_id, last_active_at DESC, id DESC) WHERE status='active'`；目的是支撑 Step 7 keyset 分页（row tuple 比较走 Index Scan + partial 完全匹配读路径）
- [ ]  downgrade 索引部分：drop 新 partial 索引 + 重建 M3-era 旧索引
- [ ]  **历史数据弃用确认**：M3 dev_[chat.py](http://chat.py) SQL 不带 `status='active'`，新索引下走 Seq Scan；不补救（dev_chat 在 M7 整体删除，dev 流量极小）。详见基线 §4.4 / 架构基线 §七
- [ ]  `alembic upgrade head` 跑通；`alembic downgrade -1 && alembic upgrade head` 验证可逆

**代码片段**

```python
# alembic/versions/<rev>_m6_messages_status_finish_reason.py
def upgrade():
    # columns: status uses PG ENUM (project convention, aligned with sessions.status)
    message_status = sa.Enum('active', 'discarded', name='messagestatus')
    message_status.create(op.get_bind(), checkfirst=True)
    op.add_column('messages', sa.Column('status', message_status, nullable=False, server_default='active'))
    op.add_column('messages', sa.Column('finish_reason', sa.String(), nullable=True))
    # drop M3-era indexes (replaced by partial indexes below)
    op.drop_index('idx_messages_session', table_name='messages')
    op.drop_index('idx_sessions_child', table_name='sessions')
    # M6 partial indexes: keyset pagination + WHERE status='active' read paths
    op.create_index(
        'idx_messages_session_active_created',
        'messages',
        ['session_id', sa.text('created_at DESC'), sa.text('id DESC')],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        'idx_sessions_child_active_lastactive',
        'sessions',
        ['child_user_id', sa.text('last_active_at DESC'), sa.text('id DESC')],
        postgresql_where=sa.text("status = 'active'"),
    )

def downgrade():
    # reverse indexes (restore M3-era)
    op.drop_index('idx_sessions_child_active_lastactive', table_name='sessions')
    op.drop_index('idx_messages_session_active_created', table_name='messages')
    op.create_index('idx_sessions_child', 'sessions', ['child_user_id', 'status'])
    op.create_index('idx_messages_session', 'messages', ['session_id', 'created_at'])
    # reverse columns
    op.drop_column('messages', 'finish_reason')
    op.drop_column('messages', 'status')
    sa.Enum(name='messagestatus').drop(op.get_bind(), checkfirst=True)
```

**验证清单**

- ✅ `alembic upgrade head` 无 error；新插入 message 默认 `status='active'`、`finish_reason=NULL`
- ✅ downgrade 可逆：两列被正确删除 + 索引回退到 M3-era 形态
- ✅ ORM 模型字段类型与 alembic 一致（`MessageStatus` PG ENUM）
- ✅ `\d messages` 验证 `status` 列类型为 `messagestatus`（不是 `text`）
- ✅ `\d messages` / `\d sessions` 验证新 partial 索引存在 + 旧索引已 drop
- ✅ `EXPLAIN SELECT ... FROM sessions WHERE child_user_id=? AND status='active' ORDER BY last_active_at DESC, id DESC LIMIT 16` 走 Index Scan（无 Sort 节点，Index Cond 含 child_user_id 等值 + row tuple 比较）
- ✅ messages keyset 查询同理走 Index Scan
- ❌ 不修改任何已有列；不重建 baseline；不动其他表（audit_records / daily_reports / auth_tokens 索引保持原状）

**Commit**

```jsx
feat(messages): add status, finish_reason columns and m6 partial indexes

- alembic: messages.status TEXT NOT NULL DEFAULT 'active'
- alembic: messages.finish_reason TEXT NULL
- alembic: drop idx_messages_session, idx_sessions_child (M3-era)
- alembic: create idx_messages_session_active_created partial WHERE status='active'
- alembic: create idx_sessions_child_active_lastactive partial WHERE status='active'
- sync ORM Message model
- accept M3 dev_chat seq-scan regression (deleted in M7)
```

---

### Step 2 · `chat/` 模块骨架 + `locks.py`

**任务**

> ⚠️ `dashscope_chat.py` / `graph.py` / `sse.py` 已在 Step 0 重构就位，`llm.py` 已删除；本步只新增 4 个业务文件骨架。
> 
- [ ]  新建 4 文件骨架：`state.py` / `context.py` / `prompts.py` / `locks.py`
- [ ]  每个文件加 module docstring + `# TODO(Step N): ...` 注释占位
- [ ]  实现 `locks.py`：`acquire_throttle_lock(child_user_id)` 1.5s SETNX；`acquire_session_lock(session_id)` 180s SETNX + nonce；`release_session_lock_lua(session_id, nonce)` Lua 脚本防误删；`running_streams: dict[str, asyncio.Event]` 模块级字典 + 文件顶部 docstring 写明「单 worker 部署约定 + 撞容量边界改造路径」
- [ ]  单测 `tests/chat/test_locks.py`：节流锁 SETNX+1.5s TTL；session 锁 SETNX+180s TTL+nonce；Lua release 错 nonce 不删

**代码片段**

```python
# backend/app/chat/locks.py
"""Locks and stop-event registry for the main dialogue stream.

Deployment contract (M6): single uvicorn worker. running_streams is an
in-process dict; cross-process stop signaling is NOT implemented. When
capacity monitors trigger (event loop lag > 50ms / mem > 3.5G / streams
> 200), upgrade path is: sticky session routing first, Redis Pub/Sub
fallback. See baseline §3.3.
"""
import asyncio
import secrets

running_streams: dict[str, asyncio.Event] = {}

RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""

async def acquire_throttle_lock(redis, child_user_id: str) -> bool:
    key = f"chat:throttle:{child_user_id}"
    return await redis.set(key, "1", nx=True, px=1500)

async def acquire_session_lock(redis, session_id: str) -> str | None:
    key = f"chat:lock:{session_id}"
    nonce = secrets.token_hex(16)
    ok = await redis.set(key, nonce, nx=True, px=180_000)
    return nonce if ok else None

async def release_session_lock(redis, session_id: str, nonce: str) -> None:
    await redis.eval(RELEASE_LOCK_LUA, 1, f"chat:lock:{session_id}", nonce)
```

**验证清单**

- ✅ `chat/` 完整 7 文件就位（4 新增 + Step 0 已有 3）；`from app.chat import locks, state, context, prompts, graph, sse, dashscope_chat` 全部可导入
- ✅ `acquire_session_lock` 同 sid 第二次返回 None；TTL 180s
- ✅ `release_session_lock` 用错 nonce 不删 key（Lua 校验生效）
- ✅ `acquire_throttle_lock` 同 child_user_id 1.5s 内第二次返回 False
- ❌ 本步只实现 `locks.py` 业务逻辑；context / prompts / state 仅骨架 + TODO；graph / sse / dashscope_chat 已在 Step 0 落地

**Commit**

```jsx
feat(chat): scaffold state/context/prompts skeletons and implement locks

- new files: state.py / context.py / prompts.py / locks.py
- locks: throttle/session SETNX + nonce + Lua release
- locks: running_streams dict (single-worker contract documented)
- tests/chat/test_locks.py: throttle/session/nonce coverage
```

---

### Step 3 · `prompts.py` 骨架（5 章节 SystemMessage + tier / gender 分发）

> ⚠️ **本步只落骨架，不锁文案**。骨架契约（拼接结构 / 字段消费 / age 5 档 / gender 4 状态 / 章节顺序）严格对齐基线 §7.3，是 M6 验收点；8 处文案位（5 章节文本 + 5 档 tier_block + 2 状态 gender_block）返回 stub 占位 + 配 `# TODO(prompts-content): 业务模板专题待定` 注释，文案内容由后续专题落地。
> 

**任务**

- [ ]  实现 `compute_age(birth_date: date, tz="Asia/Shanghai") -> int`：按 Asia/Shanghai 当天计算（仅辅助，不直接进 prompt）
- [ ]  实现 `_identity_block() -> str` / `_safety_block() -> str`：返回部署级静态 stub 文案 + `# TODO(prompts-content)` 注释
- [ ]  实现 `_tier_block(age: int) -> str`：5 档分发（`age <= 5` early_childhood / `<= 9` late_childhood / `<= 13` pre_teen / `<= 18` teen / `>= 19` young_adult），每档返回独立 stub 标记 + `# TODO(prompts-content)` 注释
- [ ]  实现 `_gender_block(gender: str | None) -> str | None`：4 状态分发，`"male"` / `"female"` 返回对应 stub 文案；`"unknown"` / `None` 返回 `None`（触发整段省略）
- [ ]  实现 `build_system_prompt(age: int, gender: str | None) -> SystemMessage`：组装 5 章节**单** SystemMessage，按基线 §7.3 顺序：
    1. `# 身份与原则\n` + `_identity_block()`
    2. `# 安全底线\n` + `_safety_block()`
    3. `# 对话风格\n` + `_tier_block(age)`
    4. `# 关于对方的性别\n` + `_gender_block(gender)`（返回 `None` 时**整段省略**，不留空标题）
    5. `# 当前对话上下文\n对方今年 {age} 岁。`（guidance 注入位由 inject_guidance 节点在此章节末尾拼接）
- [ ]  **入参契约**：`build_system_prompt` **只接受** `(age: int, gender: str | None)`，**不接受 dict / 不接受 concerns / sensitivity / custom_redlines / birth_date**（编译期签名拒绝多余字段）
- [ ]  文件顶部 docstring：「骨架已对齐基线 §7.3；5 章节文本 + tier / gender 文案待专题（grep `TODO(prompts-content)` 定位 8 处文案位）」
- [ ]  单测 `tests/chat/test_prompts.py`：
    - **结构断言**：`build_system_prompt(12, "male")` 返回 `SystemMessage`；content 含 5 章节标题且严格按序（regex 检查 `# 身份与原则` → `# 安全底线` → `# 对话风格` → `# 关于对方的性别` → `# 当前对话上下文` 顺序出现）
    - **age 字面值断言**：content 含 `对方今年 12 岁。`，且 age 字面值**仅出现在末段**
    - **tier 边界断言**：5 档 boundary（age = 3 / 5 / 6 / 9 / 10 / 13 / 14 / 18 / 19 / 21）分别命中对应 tier stub 标记字符串
    - **gender 4 状态断言**：`"male"` / `"female"` content 含 `# 关于对方的性别` 标题；`"unknown"` / `None` content **不含**该标题（整段省略生效）
    - **字段消费断言**：构造 dict 含 `concerns` / `sensitivity` / `custom_redlines` / `birth_date` 等多余字段时签名拒绝（`TypeError`）；运行期断言 content 不含这些字段名 / 值字符串
    - **compute_age 边界**：闰年 / 生日已过 / 生日未到 / 时区边界（Asia/Shanghai 当天 vs UTC 偏一日）

**代码片段**（骨架，文案占位）

```python
# backend/app/chat/prompts.py
"""System prompt builder for the main dialogue.

Skeleton aligned with baseline §7.3:
- single SystemMessage, 5 sections (L1 -> L4 cache-optimized order)
- consumes only age + gender from child_profile
- 8 content slots are stubs; grep `TODO(prompts-content)` to locate.
  The actual templates are pending a dedicated review.
"""
from datetime import date
from zoneinfo import ZoneInfo
from langchain_core.messages import SystemMessage

def compute_age(birth_date: date, tz: str = "Asia/Shanghai") -> int:
    today = date.today()  # tz-aware in real impl
    years = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years

def _identity_block() -> str:
    # TODO(prompts-content): identity & dialogue principles template
    return "[STUB identity]"

def _safety_block() -> str:
    # TODO(prompts-content): jailbreak resistance template
    return "[STUB safety]"

def _tier_block(age: int) -> str:
    if age <= 5:
        # TODO(prompts-content): early_childhood (3-5)
        return "[STUB tier:early_childhood]"
    if age <= 9:
        # TODO(prompts-content): late_childhood (6-9)
        return "[STUB tier:late_childhood]"
    if age <= 13:
        # TODO(prompts-content): pre_teen (10-13)
        return "[STUB tier:pre_teen]"
    if age <= 18:
        # TODO(prompts-content): teen (14-18)
        return "[STUB tier:teen]"
    # TODO(prompts-content): young_adult (19-21, incl. "20+")
    return "[STUB tier:young_adult]"

def _gender_block(gender: str | None) -> str | None:
    if gender == "male":
        # TODO(prompts-content): male gender block
        return "[STUB gender:male]"
    if gender == "female":
        # TODO(prompts-content): female gender block
        return "[STUB gender:female]"
    return None  # unknown / null -> omit entire section

def build_system_prompt(age: int, gender: str | None) -> SystemMessage:
    parts: list[str] = []
    parts.append(f"# 身份与原则\n{_identity_block()}")
    parts.append(f"# 安全底线\n{_safety_block()}")
    parts.append(f"# 对话风格\n{_tier_block(age)}")
    g = _gender_block(gender)
    if g is not None:
        parts.append(f"# 关于对方的性别\n{g}")
    parts.append(f"# 当前对话上下文\n对方今年 {age} 岁。")
    return SystemMessage(content="\n\n".join(parts))
```

**验证清单**

- ✅ `build_system_prompt` 签名只接受 `(age: int, gender: str | None)`；多余字段编译期签名拒绝
- ✅ content 含 5 章节标题且严格按序（身份 → 安全 → 对话风格 → 性别 → 上下文）
- ✅ age 字面值**仅出现在末段**「对方今年 X 岁。」
- ✅ tier 5 档 boundary 全部命中正确 stub（3/5/6/9/10/13/14/18/19/21）
- ✅ `"unknown"` / `None` 时 content 不含 `# 关于对方的性别`（整段省略）
- ✅ content 不含 `concerns` / `sensitivity` / `custom_redlines` / `birth_date` 字段名或值字符串
- ✅ `compute_age` 闰年 / 生日已过 / 生日未到 / 时区边界正确
- ⏸ 8 处 stub 文案待专题（本步不锁文案，仅锁结构）

**Commit**

```jsx
feat(chat): scaffold system prompt builder per baseline §7.3

- single SystemMessage, 5 sections in cache-optimized order
- consume age + gender only; reject concerns/sensitivity/custom_redlines/birth_date
- 5-tier age dispatch (3-5 / 6-9 / 10-13 / 14-18 / 19-21)
- gender 4-state dispatch (male / female / unknown->omit / null->omit)
- 8 content slots are stubs (TODO prompts-content)
- tests/chat/test_prompts.py: structure + age literal + tier boundaries + gender states + field consumption + compute_age
```

---

### Step 4 · `context.py` · `build_context`

**任务**

- [ ]  实现 `build_context(session_id, db, redis) -> list[BaseMessage]`：截最近 N=20 条 active messages（`WHERE session_id=? AND status='active' ORDER BY created_at DESC LIMIT 20`），反转为时间正序后转 `HumanMessage` / `AIMessage`
- [ ]  读 `rolling_summaries`：`SELECT * FROM rolling_summaries WHERE session_id=? LIMIT 1`；NULL 或 `turn_summaries=[]` → 不注入第二条 SystemMessage（M6 永远走 fallback）
- [ ]  **不写** `rolling_summaries` 表
- [ ]  文件顶部 docstring 标注「M6 永远 fallback；M8 审查 worker 上线后自动消费已 INSERT 的 summaries，本文件代码无需改动」
- [ ]  单测 `tests/chat/test_context.py`：空 session 返回 []；25 条 active 截最近 20 条且时间正序；status='discarded' 行被过滤；rolling_summaries NULL 走 fallback

**代码片段**

```python
# backend/app/chat/context.py
async def build_context(session_id: str, db, redis) -> list[BaseMessage]:
    rows = await db.fetch(
        "SELECT role, content FROM messages "
        "WHERE session_id=$1 AND status='active' "
        "ORDER BY created_at DESC LIMIT 20",
        session_id,
    )
    messages = [_row_to_msg(r) for r in reversed(rows)]
    summary = await db.fetchrow(
        "SELECT turn_summaries FROM rolling_summaries WHERE session_id=$1",
        session_id,
    )
    if summary and summary["turn_summaries"]:
        # M8 fallthrough: prepend summary as second SystemMessage
        ...
    return messages
```

**验证清单**

- ✅ 空 session 返回 `[]`
- ✅ 含 25 条 active messages 时只取最近 20 条且时间正序
- ✅ `status='discarded'` 行被过滤掉
- ✅ `rolling_summaries` 为 NULL 或 `turn_summaries=[]` 时不注入第二条 SystemMessage
- ❌ 不向 `rolling_summaries` 写任何数据

**Commit**

```jsx
feat(chat): add context.build_context

- sliding window N=20 active messages
- read-only rolling_summaries with fallback
- M8-ready: real summaries auto-consumed when present
- tests/chat/test_context.py: window/discard-filter/summaries-fallback
```

---

### Step 5 · `~~dashscope_chat.py` Plain Class + sse 适配~~ → 已并入 Step 0

> ⚠️ 本步全部内容已在 **Step 0**（前置基础重构）一次性落地：`dashscope_chat.py` 改 plain class、`DashScopeCallOptions` 就位、`sse.stream_to_sse` 主路径就位、`sse.stream_chat` 兼容入口保留供 dev_chat 用，单测全绿。本编号保留以维持 Step 1–11 引用稳定，**无新增 commit**。
> 

以下原 Step 5 详细内容仅作历史归档（具体落地见 Step 0）：

#### 历史归档

**任务**

- [ ]  `dashscope_chat.py`：实现 `DashScopeCallOptions`（Pydantic BaseModel）+ `ChatDashScopeQwen`（**Plain Class，不继承 BaseChatModel**）；按架构基线 §十（2026-04-30 更新）落地
- [ ]  `DashScopeCallOptions` 字段：`enable_thinking: bool = True` / `thinking_budget: int | None` / `enable_search: bool = False` / `search_options: SearchOptions | None` / `temperature` / `top_p` / `max_tokens` / `seed` / `result_format`；子模型 `SearchOptions`：`search_strategy` / `enable_source` / `forced_search` / `search_prompt`
- [ ]  `ChatDashScopeQwen.__init__(model, api_key: SecretStr)`；暴露两个方法：
    - `async def astream(messages: list[BaseMessage], *, options: DashScopeCallOptions | None = None) -> AsyncIterator[AIMessageChunk]`
    - `async def ainvoke(messages: list[BaseMessage], *, options: DashScopeCallOptions | None = None) -> AIMessage`（内部收集 `astream` 完整响应）
- [ ]  `astream` 内部：`options.model_dump(exclude_none=True)` 展开传入 `AioMultiModalConversation.call(**sdk_params)`；消息格式转换 `_to_sdk_format`（`list[dict]` 多模态格式）；错误检查 `response.status_code != HTTPStatus.OK` → 抛 `DashScopeAPIError`
- [ ]  thinking 分流：区分 `reasoning_content` / `content` 增量，分别写入 `AIMessageChunk.additional_kwargs["reasoning_content"]` / `AIMessageChunk.content`
- [ ]  `sse.py`：实现 `stream_to_sse(graph_stream, sid, hid)` 异步生成器，消费 `AIMessageChunk` 流并 yield SSE 字节：reasoning chunk 首次到达 emit `thinking_start`、末次 emit `thinking_end`、content chunk emit `delta`
- [ ]  `sse.py` 同时暴露 4 个 emit 帮手函数：`emit_session_meta(sid, hid)` / `emit_end(finish_reason, aid)` / `emit_stopped(finish_reason, aid?)` / `emit_error(code, msg)`，由 chat/stream 主入口在合适时机调用
- [ ]  SSE 帧格式：`event: <type>\ndata: <json>\n\n` 标准协议
- [ ]  单测 `tests/chat/test_dashscope_chat.py`：DashScopeCallOptions 默认值（enable_thinking=True, enable_search=False）；`model_dump(exclude_none=True)` 不含 None 字段；SearchOptions 嵌套序列化正确；mock `AioMultiModalConversation.call` → `astream` 返回 `AIMessageChunk` 流；reasoning/content 分流正确；`ainvoke` 收集完整响应；SDK 返回非 200 → 抛 `DashScopeAPIError`
- [ ]  单测 `tests/chat/test_sse.py`：reasoning chunk 流入首次 emit thinking_start；reasoning 结束 emit thinking_end 一次；content chunk emit delta；帧格式 `event: <type>\ndata: <json>\n\n`；thinking_end 不带 reasoning 文本

**代码片段**

```python
# backend/app/chat/dashscope_chat.py
"""DashScope multimodal LLM wrapper — Plain Class (not BaseChatModel).

Design decision (2026-04-30): BaseChatModel dropped because
(1) LangGraph nodes don't require it,
(2) with_structured_output / LangSmith / with_fallbacks not consumed,
(3) DashScopeCallOptions direct-pass is cleaner than bind()/model_kwargs.
See 架构基线 §十 for full rationale.

LangChain message types (AIMessageChunk etc.) are used as data containers only.
"""
from http import HTTPStatus
from pydantic import BaseModel, SecretStr
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from dashscope.aigc.multimodal_conversation import AioMultiModalConversation
from collections.abc import AsyncIterator

class SearchOptions(BaseModel):
    search_strategy: str = "pro"
    enable_source: bool = True
    forced_search: bool = False
    search_prompt: str | None = None

class DashScopeCallOptions(BaseModel):
    enable_thinking: bool = True
    thinking_budget: int | None = None
    enable_search: bool = False
    search_options: SearchOptions | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    result_format: str = "message"

class DashScopeAPIError(Exception):
    def __init__(self, code: str, message: str, request_id: str | None = None):
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(f"DashScope API error {code}: {message}")

class ChatDashScopeQwen:
    def __init__(self, model: str, api_key: SecretStr):
        self._model = model
        self._api_key = api_key

    async def astream(
        self,
        messages: list[BaseMessage],
        *,
        options: DashScopeCallOptions | None = None,
    ) -> AsyncIterator[AIMessageChunk]:
        opts = options or DashScopeCallOptions()
        sdk_params = opts.model_dump(exclude_none=True)
        sdk_messages = self._to_sdk_format(messages)
        responses = await AioMultiModalConversation.call(
            api_key=self._api_key.get_secret_value(),
            model=self._model,
            messages=sdk_messages,
            stream=True,
            incremental_output=True,
            **sdk_params,
        )
        async for chunk in responses:
            self._check_error(chunk)
            yield self._to_ai_message_chunk(chunk)

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        *,
        options: DashScopeCallOptions | None = None,
    ) -> AIMessage:
        content = ""
        reasoning = ""
        async for chunk in self.astream(messages, options=options):
            content += chunk.content or ""
            reasoning += chunk.additional_kwargs.get("reasoning_content", "")
        return AIMessage(
            content=content,
            additional_kwargs={"reasoning_content": reasoning} if reasoning else {},
        )

    @staticmethod
    def _to_sdk_format(messages: list[BaseMessage]) -> list[dict]:
        sdk_msgs = []
        for msg in messages:
            role = "system" if msg.type == "system" else (
                "user" if msg.type == "human" else "assistant"
            )
            sdk_msgs.append({"role": role, "content": [{"text": msg.content}]})
        return sdk_msgs

    @staticmethod
    def _check_error(response) -> None:
        if response.status_code != HTTPStatus.OK:
            raise DashScopeAPIError(
                code=response.code,
                message=response.message,
                request_id=getattr(response, "request_id", None),
            )

    @staticmethod
    def _to_ai_message_chunk(response) -> AIMessageChunk:
        choice = response.output.choices[0]
        msg = choice.message
        content_raw = msg.content
        # content may be str or list[dict]
        if isinstance(content_raw, list):
            text = "".join(item.get("text", "") for item in content_raw)
        else:
            text = content_raw or ""
        reasoning = getattr(msg, "reasoning_content", None) or ""
        kwargs = {}
        if reasoning:
            kwargs["reasoning_content"] = reasoning
        return AIMessageChunk(content=text, additional_kwargs=kwargs)
```

```python
# backend/app/chat/sse.py
async def stream_to_sse(graph_stream):
    thinking_started = False
    async for chunk in graph_stream:
        if isinstance(chunk, AIMessageChunk):
            r = chunk.additional_kwargs.get("reasoning_content")
            c = chunk.content
            if r and not thinking_started:
                yield _frame("thinking_start", {})
                thinking_started = True
            if not r and thinking_started:
                yield _frame("thinking_end", {})
                thinking_started = False
            if c:
                yield _frame("delta", {"content": c})

def _frame(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
```

**验证清单**

- ✅ `ChatDashScopeQwen` 是 Plain Class，**不继承 BaseChatModel**
- ✅ `DashScopeCallOptions` 是 Pydantic BaseModel；`model_dump(exclude_none=True)` 不含 None 字段
- ✅ `SearchOptions` 嵌套模型序列化正确（`search_options.model_dump()` 产出 dict）
- ✅ `astream(messages, options=opts)` 签名：options 为可选 keyword-only 参数
- ✅ `ainvoke` 内部收集 `astream` 完整响应，返回 `AIMessage`（不依赖 BaseChatModel 的 `agenerate_from_stream`）
- ✅ DashScope reasoning chunk 流入 → emit `thinking_start` 一次，再来 reasoning chunk 不重复 emit
- ✅ reasoning 流结束（首次出现 content chunk 或 reasoning 为空）→ emit `thinking_end` 一次
- ✅ content chunk → emit `delta` 含 `content` 字段
- ✅ SSE 帧格式正确：`event: <type>\ndata: <json>\n\n`
- ✅ SDK 返回非 200 → 抛 `DashScopeAPIError`（不塞进 AIMessage.content）
- ❌ thinking_end 不带 reasoning 文本内容（基线 §3.2 关键设计：仅传信号不传文本）
- ❌ 不继承 BaseChatModel、不实现 `_astream` / `_generate` / `_llm_type` 等协议方法

**Commit**

```jsx
feat(chat): add plain-class ChatDashScopeQwen + DashScopeCallOptions + sse adapter

- DashScopeCallOptions Pydantic model (enable_thinking / enable_search / search_options etc.)
- ChatDashScopeQwen plain class: astream + ainvoke, NOT BaseChatModel
- thinking-mode split: reasoning_content → additional_kwargs, content → content
- sse: thinking_start/thinking_end/delta from graph stream
- sse: emit_session_meta/end/stopped/error helpers
- tests/chat/test_dashscope_chat.py: options serialization + stream split + error handling
- tests/chat/test_sse.py: thinking split + frame format
```

---

### Step 6 · `state.py` + `graph.py` · 主对话图装配

**任务**

- [ ]  `state.py`：定义 LangGraph TypedDict（`session_id` / `child_user_id` / `child_profile` / `messages` / `audit_state` / `generated_token_count` / `client_alive` / `user_stop_requested` 等字段）
- [ ]  `graph.py`：实现 **5 节点 + 1 router**（持久化与 enqueue 收敛到 generator，**不进图**）：
    - `load_audit_state`（M6 恒返回空 dict，M8 改读 Redis `audit:{sid}`）
    - `route_by_risk` **conditional router**（**5 信号 → 4 路由输出**：① `crisis_locked=true`（粘性，最高优先级）→ `crisis` ② 本轮 `crisis_detected` → `crisis` ③ 本轮 `redline_triggered` → `redline` ④ `guidance != None` → `guidance`（**进 inject_guidance 前置节点**）⑤ else → `main`；M6 阶段 audit_state 恒空 + guidance 恒 None → 恒走 ⑤ main 分支；详见基线 §7.1.1 / §7.1.2）
    - `call_main_llm`（调 `ChatDashScopeQwen.astream`，传 `build_system_prompt + build_context + 当轮 human + 可选注入后的 messages`，通过 LangGraph `writer` 透出 `AIMessageChunk` 流）
    - `call_crisis_llm` stub（走主 LLM 兜底 + `logger.warning("M6 stub crisis_llm fallback to main")`）
    - `call_redline_llm` stub（同上）
    - `inject_guidance`（**位于 ④ guidance 分支的前置节点，在 call_main_llm 之前**；非 LLM 调用 / 仅 messages 数组改写：**找到最后一条 HumanMessage，在其前插入独立 `SystemMessage(content=audit_state.guidance)`**；**不**拼进首条 SystemMessage（基线 §7.5 弱注入语义 + recency 权重最大化 + 避免污染 prompt cache 命中区）；guidance **不**落 `messages` 表；M6 阶段 audit_state 恒空 → 本节点恒不被触发，代码完整就位 M8 起生效）
- [ ]  **持久化与 enqueue_audit 不进图**：`persist_ai_turn(db, sid, finish_reason, content)` 与 `enqueue_audit(sid)` 作为 helper 函数放在 `graph.py`（或独立 `persistence.py`）顶层导出，由 [me.py](http://me.py) generator 在合适时机调用（见 Step 8b/8c）。**T5 唯一写入点 = generator**，graph 节点不写 ai 行
- [ ]  图边：`START → load_audit_state → route_by_risk → {call_crisis_llm | call_redline_llm | inject_guidance | call_main_llm} → END`；inject_guidance 后接 call_main_llm；三个 LLM 节点（main/crisis/redline）均直接 → END。compile 后导出 `main_graph`
- [ ]  单测 `tests/chat/test_graph.py`：**route_by_risk 5 信号优先级**：① `crisis_locked=true` → "crisis" / ② `crisis_detected=true` → "crisis" / ③ `redline_triggered=true` → "redline" / ④ `guidance != None` → "guidance" / ⑤ else → "main"；**优先级断言**：crisis_locked + redline_triggered 同时命中走 "crisis"（crisis 优先级高于 redline）；M6 `audit_state=\{\}` + `guidance=None` 恒走 ⑤ main；**图边断言**：编译后图中 `inject_guidance → call_main_llm`，三个 LLM（main / crisis / redline）都直接 → `END`，图内**不**写库；**inject_guidance 行为断言**：传入含 `[Sys, Hum A, AI B, Hum C]` 的 messages 与 guidance="X"，返回 `[Sys, Hum A, AI B, Sys(X), Hum C]`（在最后一条 HumanMessage 前插入独立 SystemMessage，不动首条 Sys）；stub 节点（crisis / redline）走主 LLM 兜底且 logger.warning 输出
- [ ]  单测 `tests/chat/test_persistence.py`：`persist_ai_turn` helper 写入 ai active + finish_reason + content；`enqueue_audit` helper M6 no-op + logger.warning 输出

**代码片段**

```python
# backend/app/chat/graph.py
builder = StateGraph(MainDialogueState)
builder.add_node("load_audit_state", load_audit_state)
builder.add_node("call_main_llm", call_main_llm)
builder.add_node("call_crisis_llm", call_crisis_llm)  # stub
builder.add_node("call_redline_llm", call_redline_llm)  # stub
builder.add_node("inject_guidance", inject_guidance)

builder.set_entry_point("load_audit_state")

# 5 信号 -> 4 路由输出（基线 §7.1）：
#   ① crisis_locked / ② crisis_detected -> "crisis"
#   ③ redline_triggered                 -> "redline"
#   ④ guidance != None                  -> "guidance"  (pre-call_main_llm)
#   ⑤ else (M6 默认)                    -> "main"
builder.add_conditional_edges("load_audit_state", route_by_risk, {
    "crisis": "call_crisis_llm",
    "redline": "call_redline_llm",
    "guidance": "inject_guidance",
    "main": "call_main_llm",
})

# ④ guidance 分支：inject_guidance 是 call_main_llm 的前置节点（基线 §7.1.2）
builder.add_edge("inject_guidance", "call_main_llm")

# 三个 LLM 节点直接到 END；持久化与 enqueue_audit 由 me.py generator 处理
# （T5 唯一写入点 = generator，图内不做 DB 写入）
builder.add_edge("call_main_llm", END)
builder.add_edge("call_crisis_llm", END)
builder.add_edge("call_redline_llm", END)

main_graph = builder.compile()
```

**验证清单**

- ✅ `main_graph.astream(initial_state, stream_mode="custom")` 跑通，单条 message 进入 → 流式返回 `AIMessageChunk`
- ✅ M6 阶段 `audit_state={}` + guidance=None 时恒走 ⑤ main 分支
- ✅ **图边正确**：`inject_guidance → call_main_llm`；三个 LLM（main / crisis / redline）均直接 → `END`，图内**无**写库节点
- ✅ route_by_risk 5 信号优先级断言全绿（含 crisis_locked + redline 同时命中走 crisis 的优先级）
- ✅ inject_guidance 在最后一条 HumanMessage 前插入独立 SystemMessage（不污染首条 SystemMessage）
- ✅ stub 节点（crisis / redline）走主 LLM 兜底且 logger.warning 输出
- ✅ `persist_ai_turn` / `enqueue_audit` helper 单测全绿（不在图内调用）
- ❌ M6 阶段不调真实 crisis / redline LLM（M9 替换 stub 内部）
- ❌ inject_guidance 不写首条 SystemMessage、不写 `messages` 表
- ❌ graph 内**不**做 DB 写入（**T5 唯一写入点 = generator**）

**Commit**

```jsx
feat(chat): assemble main dialogue graph (5 nodes, no DB writes)

- state: MainDialogueState TypedDict
- 5 graph nodes: load_audit_state / call_main_llm / call_crisis_llm /
  call_redline_llm / inject_guidance (+ route_by_risk router)
- persist_ai_turn / enqueue_audit moved to helpers, called from
  me.py generator (T5 single write point)
- conditional routing 5 signals -> 4 outputs (crisis / redline /
  guidance / main); inject_guidance is pre-call_main_llm in guidance
  branch (baseline §7.1.2)
- inject_guidance: insert independent SystemMessage before the last
  HumanMessage; do NOT touch the head SystemMessage (baseline §7.5)
- stub nodes: crisis_llm / redline_llm fallback to main + warning
- tests: graph routing + edge assertions + inject_guidance behavior +
  persistence helpers
```

---

### Step 7 · `me.py` · 3 个轻端点（GET sessions / GET messages / DELETE session）

> ⚠️ **路径前缀**：现网 `me.py` 是 `APIRouter(prefix="/api/v1/me")`；本步及 Step 8 / 9 的端点路径文字省略 `/api/v1` 前缀，实际访问路径为 `/api/v1/me/...`，沿用现网 router 不再新建。
> 

**任务**

- [ ]  `GET /me/sessions`（**keyset 分页，非 offset**）：查询参数 `cursor` (str, optional, base64) + `limit` (int, default=15, max=50)（默认值已与基线 §3.1 锁定为 15）；响应 `{items: [{id, title, last_active_at}], next_cursor: str | null}`；排序 `(last_active_at DESC, id DESC)`；`WHERE child_user_id=? AND status='active'`，cursor 非空时追加 `AND (last_active_at, id) < (cursor.last_active_at, cursor.id)`；cursor 编码 `base64(f"{last_active_at_iso}|{id}")`，对客户端不透明；查询 `LIMIT limit + 1` 探测 has_more，溢出那条作为 `next_cursor`，末页 `next_cursor=null`；**不返回 `in_progress`**、**不探测 Redis**
- [ ]  `GET /me/sessions/{id}/messages`（**keyset 分页，非 offset**）：查询参数 `cursor` (str, optional, base64) + `limit` (int, default=50, max=100)；响应 `{items: [...], next_cursor: str | null, in_progress: bool}`；排序 `(created_at DESC, id DESC)` —— 首屏取最新 N 条，向上翻历史靠 cursor；`WHERE session_id=? AND status='active'`，cursor 非空时追加 `AND (created_at, id) < (cursor.created_at, cursor.id)`；cursor 编码 `base64(f"{created_at_iso}|{id}")`，对客户端不透明；前端展示时反转为时间正序；403 child 不匹配 / 404 session 不存在；**响应顶层** `in_progress = bool(await redis.exists(f"chat:lock:{id}"))`
- [ ]  `DELETE /me/sessions/{id}`：`UPDATE sessions SET status='deleted' WHERE id=? AND child_user_id=?`；不物理删 messages；403/404 处理；响应 204
- [ ]  所有端点鉴权用 `Depends(require_child_token)` 已有依赖
- [ ]  单测 `tests/api/test_me_sessions.py`：cursor 编解码可逆；keyset 翻页 happy path（首页 → next_cursor → 末页 null）；limit 边界（默认 / 最大 / 超限 422）；status='discarded' 行被过滤；DELETE 软删后 GET 返回 404；child 不匹配 403；session 不存在 404；in_progress 锁存在 true / 锁不存在 false

**代码片段**

```python
# backend/app/api/me.py
import base64

def _encode_cursor(sort_key_iso: str, row_id: str) -> str:
    return base64.urlsafe_b64encode(f"{sort_key_iso}|{row_id}".encode()).decode()

def _decode_cursor(cursor: str) -> tuple[str, str]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    sort_key, row_id = raw.rsplit("|", 1)
    return sort_key, row_id

@router.get("/me/sessions")
async def list_sessions(
    limit: int = Query(15, ge=1, le=50),
    cursor: str | None = None,
    user = Depends(require_child_token),
    db = Depends(get_db),
):
    args = [user.id]
    where = "child_user_id=$1 AND status='active'"
    if cursor:
        last_active_at, sid = _decode_cursor(cursor)
        args += [last_active_at, sid]
        where += " AND (last_active_at, id) < ($2, $3)"
    rows = await db.fetch(
        f"SELECT id, title, last_active_at FROM sessions WHERE {where} "
        f"ORDER BY last_active_at DESC, id DESC LIMIT {limit + 1}", *args)
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = _encode_cursor(items[-1]["last_active_at"].isoformat(), items[-1]["id"]) if has_more else None
    return {"items": items, "next_cursor": next_cursor}

@router.get("/me/sessions/{sid}/messages")
async def get_messages(sid: str, limit: int = Query(50, ge=1, le=100), cursor: str | None = None, ...):
    # 403/404 checks first
    # ... build where with optional (created_at, id) < cursor tuple
    # ... ORDER BY created_at DESC, id DESC LIMIT limit + 1
    in_progress = bool(await redis.exists(f"chat:lock:{sid}"))
    return {"items": items, "next_cursor": next_cursor, "in_progress": in_progress}
```

**验证清单**

- ✅ `GET /me/sessions` 默认返回 15 条；`limit=50` OK；`limit=51` 返回 422
- ✅ `GET /me/sessions` 不返回 `in_progress` 字段
- ✅ `GET /me/sessions` keyset 翻页：第二次传响应的 `next_cursor` → 返回更早一页 + 新 `next_cursor`；末页 `next_cursor=null`
- ✅ `GET /me/sessions/{id}/messages` 顶层含 `in_progress`，session 锁存在时为 `true`
- ✅ `GET /me/sessions/{id}/messages` keyset 翻页：cursor 不传 → 最新 50 条；传 cursor → 更早 50 条
- ✅ cursor 用 `(排序键, id)` 联合，相同时间戳多行不漏页不重复
- ✅ `DELETE /me/sessions/{id}` 后 `GET /me/sessions/{id}/messages` 返回 404；messages 行物理上仍存在
- ✅ child 不匹配 → 403；session 不存在 → 404
- ❌ messages 端点 `WHERE` 条件必须含 `status='active'`，不返回 discarded 行

**Commit**

```jsx
feat(api): add me sessions list, messages, soft-delete endpoints

- GET /me/sessions: paginated, no in_progress field
- GET /me/sessions/{id}/messages: status='active' + top-level in_progress
- DELETE /me/sessions/{id}: soft delete via status='deleted'
- tests/api/test_me_sessions.py: pagination + cursor + soft-delete + in_progress
```

---

### Step 8a · `me.py` · `POST /me/chat/stream` 控制平面 + stub 流

> ⚠️ **Step 8 三段拆分的第 1 段**：实装锁 / 末行决策矩阵 / T2 / title / session_meta / 释锁，LLM 流用 stub generator 占位（在 8b 替换）。
> 

**任务**

- [ ]  **请求校验**：`content: str`（重生时空串）+ `session_id: str | None` + `regenerate_for: str | None`
- [ ]  **节流锁**：`chat:throttle:{child_user_id}` SETNX 1.5s；抢不到 → 429 RequestThrottled
- [ ]  **session 锁**：首轮内存生成 sid → SETNX；非首轮先 SELECT session 校验存在性 + child 匹配（404/403），再 SETNX；抢不到 → 409 SessionBusy
- [ ]  **末行检查 + 末行决策矩阵**（基线 §5.4）：`SELECT ... ORDER BY created_at DESC LIMIT 1`；按 `(末行 role, regenerate_for)` 9 行决策分支；孤儿 human 改内容重发 → `UPDATE 旧 human SET status='discarded'` + INSERT 新 human
- [ ]  **首段事务**：首轮 INSERT session（含 title 截取）+ INSERT human active；非首轮按末行决策矩阵 结果 INSERT/复用 human
- [ ]  **title 生成**：截 `user_content` 前 12 字符按 grapheme 边界（用 `regex` 库 `\X` 模式，需 `pip install regex` 加到 requirements）
- [ ]  **emit `session_meta`**：首段事务 commit 后立刻发
- [ ]  **stub generator**：`async def _stub_stream(aid): yield emit_delta("[stub]"); yield emit_end("stop", aid=aid)` 占位，在 8b 替换为真实 graph 流
- [ ]  **finally 释锁**：`release_session_lock(redis, sid, nonce)` Lua（running_streams 注册留 8c）
- [ ]  **child_profile 装载到 initial_state**：在 `Depends(require_child)` 拿到 [user.id](http://user.id) 后，复用 [me.py](http://me.py) 已有的 `ChildProfile` 查询（`SELECT * FROM child_profiles WHERE child_user_id=?`），把 `gender` + `compute_age(birth_date)` 拼成 `child_profile` 字典塞入传给 `main_graph.astream` 的 `initial_state`；child_profile 不存在 → 500（前置鉴权应已保证存在，此为异常态）。**该字段是 build_system_prompt(age, gender) 的唯一数据源**
- [ ]  **regenerate_for 语义锁定**：请求体 model 上注释明确「regenerate_for 必须为该 session 当前末行 active 的 human message id；指向更早的 human / 指向 ai / 行不存在 → 400 RegenerateForInvalid」；末行决策矩阵 9 行覆盖该校验
- [ ]  在 `pyproject.toml` 的 `[project].dependencies` 加 `regex`，再跑 `uv pip freeze > requirements.txt` 重生 lock（项目用 uv 管理依赖；`requirements.txt` 是 freeze 产物，**禁止手编**）
- [ ]  单测 `tests/api/test_chat_stream_control_plane.py`：末行决策矩阵 9 行全覆盖（不存在/null、不存在/非null、ai/null、ai/非null、孤儿/null、孤儿/=hid、孤儿/≠hid、session 不存在、child 不匹配）；节流锁 1s 内连发两次第二次 429；session 锁同 sid 锁未释放发第二次 409；title 12 grapheme 截取（ASCII / 中文 / emoji ZWJ / 组合字符）；首段事务正确写入 session+human active；session_meta 帧 首段事务 commit 后立刻发；finally 锁正确释放

**代码片段**

```python
# backend/app/api/me.py
@router.post("/me/chat/stream")
async def chat_stream(req: ChatStreamRequest, user=..., db=..., redis=...):
    if not await acquire_throttle_lock(redis, user.id):
        raise HTTPException(429, "RequestThrottled")
    sid = req.session_id or generate_uuid()
    if req.session_id:
        # 404/403 checks via SELECT
        ...
    nonce = await acquire_session_lock(redis, sid)
    if not nonce:
        raise HTTPException(409, "SessionBusy")
    # 末行决策矩阵 matrix → 首段事务 (session+human active) → title 截取
    hid, aid = await run_decision_o_and_t2(db, sid, req)

    async def generator():
        try:
            yield emit_session_meta(sid, hid)
            # TODO(8b): replace stub with main_graph.astream + stream_to_sse
            async for sse in _stub_stream(aid):
                yield sse
        finally:
            await release_session_lock(redis, sid, nonce)

    return StreamingResponse(generator(), media_type="text/event-stream")
```

**验证清单**

- ✅ 首轮：sid null + content 非空 → 200 + SSE session_meta + stub delta + end + DB 末态 = session active + human active
- ✅ 节流锁触发：1s 内连发两次 → 第二次 429
- ✅ session 锁触发：同 sid 锁未释放发第二次 → 409
- ✅ 末行决策矩阵 9 行在 mock DB 下全部命中预期分支
- ✅ title 截取：`"Hello 你好 👨‍👩‍👧 abc"` → 前 12 grapheme（emoji ZWJ 序列算 1 个）
- ✅ 锁在 finally 释放（Lua 校验 nonce）
- ❌ 本步不接 graph、不写 ai 行、不实现 stop / 不 cancel（留 8b/8c）

**Commit**

```jsx
feat(api): add me chat stream endpoint control plane (stub stream)

- throttle/session locks + Lua release
- decision O 9-row last-row check matrix
- 首段事务 (session+human active) + title from 12 graphemes via regex \X
- emit session_meta after 首段事务 commit
- stub generator placeholder (replaced in 8b)
- tests/api/test_chat_stream_control_plane.py: decision-O matrix + locks + title + T2
```

---

### Step 8b · `me.py` · 接入 LangGraph 主图 + T5

> ⚠️ **Step 8 三段拆分的第 2 段**：把 8a 的 stub generator 替换为真实 graph 流 + SSE 适配 + 末段事务。stop / 不 cancel 留 8c。
> 

**任务**

- [ ]  删除 8a 的 `_stub_stream`，替换为 `async for chunk in main_graph.astream(initial_state, stream_mode="custom")`
- [ ]  用 `stream_to_sse(...)` 包裹 chunk 流，emit `thinking_start` / `thinking_end` / `delta`
- [ ]  边消费边累积 `accumulated_content`：从每个 chunk 的 `chunk.content` 累加（reasoning_content **不**入库）
- [ ]  **末段事务（T5 唯一写入点）**：流自然结束 → 从消费过程累积的 `last_finish_reason`（兜底 `'stop'`，每条 chunk 的 `response_metadata["finish_reason"]` 命中时覆盖）取真实值，调用 `persist_ai_turn(db, sid, finish_reason=last_finish_reason, content=accumulated)` helper（INSERT ai active + UPDATE sessions.last_active_at）；`emit_end` 也用真实值。**graph 内不再有 persist_turn 节点**（Step 6 修订），ai 行只在此处写入一次
- [ ]  **emit `end`**：末段事务 commit 后发，含 aid
- [ ]  **emit `error`**：try/except 捕获 graph 内部异常 → emit error 帧 + 不写 ai 行（保留 human active）+ 释锁
- [ ]  单测 `tests/api/test_chat_stream_graph.py`：mock `main_graph.astream` 返回 reasoning + content 双流 → SSE 顺序 `session_meta → thinking_start → thinking_end → delta×N → end`；末段事务写 ai active + 真实 finish_reason + last_active_at 更新；**finish_reason 三态覆盖**（mock 末帧分别为 `stop` / `length` / `content_filter` → DB 行 + SSE `end` 帧均落对应值）；graph 抛异常 → SSE error 帧 + DB 末态末行 human active（A4）+ 锁释放

**代码片段**

```python
# backend/app/api/me.py (内 generator 替换 8a stub)
async def generator():
    accumulated = ""
    last_finish_reason = "stop"  # 兜底；末帧 chunk.response_metadata 命中时覆盖
    try:
        yield emit_session_meta(sid, hid)
        async for chunk in main_graph.astream(initial_state, stream_mode="custom"):
            if chunk.content:
                accumulated += chunk.content
            fr = (chunk.response_metadata or {}).get("finish_reason")
            if fr:
                last_finish_reason = fr  # stop / length / content_filter
            async for sse in stream_to_sse([chunk]):
                yield sse
        # T5 唯一写入点: INSERT ai active + finish_reason (真实值) + content + UPDATE last_active_at
        aid = await persist_ai_turn(db, sid, finish_reason=last_finish_reason, content=accumulated)
        yield emit_end(last_finish_reason, aid=aid)
    except Exception as e:
        yield emit_error("internal", str(e))
    finally:
        await release_session_lock(redis, sid, nonce)
```

**验证清单**

- ✅ 正常流：mock graph 返回 thinking + content → SSE `session_meta → thinking_start → thinking_end → delta×N → end` 按序到达
- ✅ T5 写入：ai active + finish_reason=真实值（mock 末帧 `stop` / `length` / `content_filter` 应分别命中并落库 + 落 SSE `end` 帧）+ content=累积值，sessions.last_active_at 更新；**全链路只 INSERT 一次**（无双写）
- ✅ graph 异常：SSE emit error + 末行保持 human active + 锁释放
- ✅ 8a 控制平面单测仍全绿（回归）
- ❌ 本步不实现 stop / 不 cancel（留 8c）

**Commit**

```jsx
feat(api): wire main graph stream and t5 persist

- replace stub generator with main_graph.astream + stream_to_sse
- 末段事务 (ai active + finish_reason from sdk last chunk + last_active_at)
- error path: emit error frame + preserve human active
- tests/api/test_chat_stream_graph.py: 7-event sequence + T5 + error path
```

---

### Step 8c · `me.py` · stop 检测 + 不 cancel + StopKind 二分支

> ⚠️ **Step 8 三段拆分的第 3 段**：注册 running_streams + stop event 检测 + 客户端断连 不 cancel + StopKind 二分支。
> 

**任务**

- [ ]  **注册 running_streams**：emit session_meta 后 `event = asyncio.Event(); running_streams[sid] = event`
- [ ]  **stop 检测**：每个 yield 前 `if event.is_set(): break`
- [ ]  **StopKind 二分支**（基线 §5.2）：根据 `has_emitted_content` 标志（**非** chunk.token_count；AIMessageChunk 中间帧无 token 字段，usage_metadata 仅末帧才有）：
    - StopNoAi（never emitted any non-empty content）：不写 ai 行；末态末行 = human active；emit `stopped` 不含 aid
    - StopWithAi（has_emitted_content=True）：调用 `persist_ai_turn(... finish_reason='user_stopped', content=accumulated)`；emit `stopped` 含 aid
- [ ]  **不 cancel 原则**（基线 §5.3）：包 `try/except ConnectionError` 捕获客户端断连 → 置 `client_alive=False` 后续 yield 改 noop（LLM 流不 cancel）
- [ ]  **finally 补全**：`running_streams.pop(sid, None)`（锁释放在 8a 已实现）
- [ ]  单测 `tests/api/test_chat_stream_stop_keepgo.py`：event.set() → generator 在下次 yield 退出；StopNoAi（thinking 中调 stop）→ DB 末行 human active + SSE stopped 无 aid；StopWithAi（delta 中调 stop）→ DB 末行 ai active + finish_reason='user_stopped' + SSE stopped 含 aid；ConnectionError 模拟 → LLM 流不 cancel + 末段事务仍写入 + 锁释放

**代码片段**

```python
# backend/app/api/me.py (内 generator 增量)
async def generator():
    client_alive = True
    event = asyncio.Event()
    running_streams[sid] = event
    has_emitted_content = False
    accumulated = ""
    last_finish_reason = "stop"  # 兜底；末帧 chunk.response_metadata 命中时覆盖
    try:
        yield emit_session_meta(sid, hid)
        async for chunk in main_graph.astream(initial_state, stream_mode="custom"):
            if event.is_set():
                break
            if chunk.content:
                has_emitted_content = True
                accumulated += chunk.content
            fr = (chunk.response_metadata or {}).get("finish_reason")
            if fr:
                last_finish_reason = fr
            async for sse in stream_to_sse([chunk]):
                if not client_alive:
                    continue  # 不 cancel: yield noop, 但仍消费 LLM 流
                try:
                    yield sse
                except (ConnectionError, anyio.BrokenResourceError, asyncio.CancelledError):
                    client_alive = False
        if event.is_set():
            # StopKind 二分支: 按"是否吐过非空 content"判, 不依赖 token 计数
            if not has_emitted_content:
                yield emit_stopped(finish_reason="user_stopped")  # StopNoAi
            else:
                aid = await persist_ai_turn(db, sid, finish_reason="user_stopped", content=accumulated)
                yield emit_stopped(finish_reason="user_stopped", aid=aid)  # StopWithAi
        else:
            # 自然结束: 用累积的真实 finish_reason (stop / length / content_filter)
            aid = await persist_ai_turn(db, sid, finish_reason=last_finish_reason, content=accumulated)
            yield emit_end(last_finish_reason, aid=aid)
    except Exception as e:
        yield emit_error("internal", str(e))
    finally:
        await release_session_lock(redis, sid, nonce)
        running_streams.pop(sid, None)
```

**验证清单**

- ✅ event.set() 触发 generator 在下次循环退出
- ✅ StopNoAi：未吐过非空 content 调 stop → 末行 human active + SSE stopped 不含 aid
- ✅ StopWithAi：已吐过 content 调 stop → 末行 ai active + finish_reason='user_stopped' + content=累积值 + SSE stopped 含 aid
- ✅ 不 cancel：客户端断连 → LLM 流不 cancel + 末段事务仍写入 + 锁释放
- ✅ running_streams 在 finally 清理
- ✅ 8a/8b 单测仍全绿（回归）

**Commit**

```jsx
feat(api): add stop detection, keepgo, and stopkind branching

- register running_streams + event-driven break
- StopKind: StopNoAi / StopWithAi by has_emitted_content flag (NOT chunk.token_count)
- 不 cancel: try/except ConnectionError → yield noop, no LLM cancel
- finally: pop running_streams
- tests/api/test_chat_stream_stop_keepgo.py: stopkind + keepgo + connection-error
```

---

### Step 9 · `me.py` · `POST /me/sessions/{id}/stop` + `dev_chat.py` DEPRECATED banner

**任务**

- [ ]  实现 `POST /me/sessions/{id}/stop`：鉴权 + child 校验 + session 存在性校验 → `event = running_streams.get(sid); if event: event.set()` → 立即返回 204；无论 event 是否存在都返回 204（best-effort）
- [ ]  dev_[chat.py](http://chat.py) 的 DEPRECATED docstring + load-time `logger.warning` **已在 Step 0 加好**，本步**不再重复加 banner**
- [ ]  仅补全 `dev_chat.py` 文件级 docstring 中的「TODO(M7 cleanup): 整文件删除 + [main.py](http://main.py) 路由注册去除（基线 §7.6）」（如 Step 0 未写则此处补，已写则跳过）
- [ ]  单测 `tests/api/test_stop.py`：stop 接口对不存在 sid 返回 404；其他 child 的 sid 返回 403；running_streams 命中时 event.set 触发 generator 退出（与 8c event 检测联动）；不存在 running_streams entry 时仍返回 204 best-effort

**代码片段**

```python
# backend/app/api/me.py
@router.post("/me/sessions/{sid}/stop", status_code=204)
async def stop(sid: str, user=Depends(require_child_token), db=Depends(get_db)):
    # 401 in dependency, then validate child + session existence (403/404)
    session = await db.fetchrow(
        "SELECT child_user_id FROM sessions WHERE id=$1 AND status='active'", sid)
    if not session:
        raise HTTPException(404, "SessionNotFound")
    if session["child_user_id"] != user.id:
        raise HTTPException(403, "SessionForbidden")
    event = running_streams.get(sid)
    if event:
        event.set()
    # Always return 204 (async best-effort)
```

```python
# backend/app/api/dev_chat.py
"""DEPRECATED — will be removed in M7 (cleanup contract). Use /me/chat/stream.

TODO(M7 cleanup): delete this file + remove route registration from main.py.
See baseline §7.6.
"""
import logging
logging.getLogger(__name__).warning(
    "dev_chat module loaded; this endpoint is DEPRECATED and will be removed in M7"
)
```

**验证清单**

- ✅ stop 接口 → running_streams 命中时 generator 在下次 yield 退出
- ✅ stop 接口对不存在的 sid 返回 404；对其他 child 的 sid 返回 403
- ✅ stop 后 DB 末态正确：StopNoAi（未吐过非空 content）末行 human / StopWithAi（已吐过 content）末行 ai active + finish_reason='user_stopped'
- ✅ stop 接口异步生效：返回 204 时 event 已 set，generator 后续退出由 chat/stream finally 处理
- ✅ dev_chat 模块加载时 logger.warning 输出一次
- ✅ dev_chat 路由仍可访问（M6 不破坏 dev hub）

**Commit**

```jsx
feat(api): add chat stop endpoint

- POST /me/sessions/{id}/stop: 204 best-effort, event.set()
- dev_chat.py: TODO(M7 cleanup) note in docstring (banner already in Step 0)
- tests/api/test_stop.py: 404/403 + event.set + best-effort 204
```

---

### Step 10 · mock LLM 集成测

**任务**

- [ ]  `tests/integration/test_chat_stream.py`：HTTP → SSE 全链路；mock `ChatDashScopeQwen.astream` 返回构造的 `AIMessageChunk` 流（含 reasoning + content 双流）
- [ ]  用例覆盖：
    1. **正常流**：入口①草稿首发首轮 → 7 事件齐全（session_meta + thinking_start + thinking_end + delta×N + end）+ DB 末态正确
    2. **LLM 失败**：mock raise → SSE error 帧 + DB 末态末行 human（失败④AI 失败态）+ 锁释放
    3. **regenerate 三场景**：复用孤儿 / 改内容重发 / 历史轮拒绝 400
    4. **StopNoAi**：thinking 中调 stop → DB 末行 human + SSE stopped 无 aid
    5. **StopWithAi**：delta 中调 stop → DB 末行 ai + finish_reason=user_stopped + SSE stopped 含 aid
    6. **Resume in_progress**：锁存在时 GET messages 顶层 `in_progress=true`；锁释放后 false
    7. **客户端断连**：模拟 ConnectionError → LLM 流不 cancel + 末段事务仍写入
    8. **节流锁**：1s 内连发 2 次 → 第二次 429
    9. **session 锁**：同 sid 双发 → 第二次 409
- [ ]  用 `httpx.AsyncClient` + `pytest-asyncio`；用 `unittest.mock.patch` 替换 `ChatDashScopeQwen`

**代码片段**

```python
# tests/integration/test_chat_stream.py
@pytest.mark.asyncio
async def test_normal_first_turn(client, mock_llm):
    mock_llm.set_stream([
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking..."}),
        AIMessageChunk(content="Hello "),
        AIMessageChunk(content="world"),
    ])
    events = await collect_sse(client.post("/me/chat/stream", json={...}))
    assert [e["event"] for e in events] == [
        "session_meta", "thinking_start", "thinking_end", "delta", "delta", "end"
    ]
```

**验证清单**

- ✅ 9 用例全绿
- ✅ SSE 帧顺序与 §3.2 一致
- ✅ 覆盖基线 §二大图所有有终态分支（失败①HTTP 异常除外，网络层不进服务端）
- ❌ 不依赖真 DashScope API

**Commit**

```jsx
test(api): end-to-end sse integration tests with mocked llm

- 9 cases covering normal/error/stop/regenerate/resume/throttle/session-busy
- ConnectionError simulation for 不 cancel verification
```

---

### Step 11 · 验收 + 偏差登记

**任务**

- [ ]  对照 [M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21) 逐节验收：
    - §3.1 5 端点契约
    - §3.2 SSE 7 事件
    - §3.3 stop 接口 + 部署约定 docstring 落实
    - §4.1-4.3 schema 字段 + in_progress 在 messages 接口顶层
    - §5.1 双锁
    - §5.2 StopKind 二分支
    - §5.3 不 cancel
    - §5.4 末行决策矩阵
    - §6.4 Resume 三分支（messages 顶层 `in_progress` 数据来源）
    - §3.4 错误码矩阵：429 RequestThrottled / 409 SessionBusy / 404 SessionNotFound / 403 SessionForbidden / 400 RegenerateForInvalid / 500 internal —— 每个错误码至少 1 个集成测命中
    - §7.1-7.5 graph 节点骨架 + build_context + system prompt
    - §7.6 M7/M8/M9 清理点全部加 `# TODO(...)` 注释（grep 抽查）
- [ ]  执行偏差登记：发现的设计偏差 / 妥协项 / 后续优化点写入 [](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)，并在本页「五、发现与建议」节摘要登记
- [ ]  创建 PR：`feat/m6-main-chat-backend → main`；PR description 引用本计划页 + 列 13 步 commits（8 拆 8a/8b/8c）+ 验收清单截图
- [ ]  等 review pass 后 squash merge，回到 main 验证 `pytest` 全绿 + `alembic upgrade head`

**验证清单**

- ✅ 基线逐节验收无遗漏（含 §3.4 错误码矩阵 6 类全部命中）
- ✅ M7/M8/M9 清理点 grep 命中 6 处（dev_chat / dev-chat.tsx 引用 / 旧 SSE 协议 / enqueue_audit / crisis+redline / build_context summaries）
- ✅ PR description 完整
- ⏸ system prompt 模板替换不在本 PR 范围（专题讨论后单独 commit）

**Commit**（如有偏差或文档调整）

```
docs(m6): record verification results and deviations
```

## 四、验收清单（汇总）

端到端验收点（与基线契约对齐）：

- [ ]  **5 端点全部就位**：list / messages / delete / chat-stream / stop
- [ ]  **SSE 7 事件**：session_meta / thinking_start / thinking_end / delta / end / stopped / error
- [ ]  **双锁**：节流 1.5s + session 180s + Lua nonce DEL
- [ ]  **末行决策矩阵 9 行**：单测 + 集成测全覆盖
- [ ]  **Resume 路径**：`GET /me/sessions/{id}/messages` 顶层 `in_progress` 探测正确
- [ ]  **不 cancel 原则**：客户端断连不 cancel LLM；末段事务仍写入；锁正常释放
- [ ]  **title 截取**：12 grapheme 边界（含 emoji ZWJ）
- [ ]  **DEPRECATED banner**：dev_chat 路由可用 + load-time logger.warning
- [ ]  **TODO 注释纪律**：6 处清理点（M7/M8/M9）grep 命中
- [ ]  **测试**：单测 + mock 集成测全绿；coverage 关键模块 > 85%
- [ ]  **alembic**：upgrade / downgrade 可逆
- [ ]  **部署约定 docstring**：`locks.py` 顶部写明单 worker 约定 + 监控触发条件 + 改造路径

## 五、发现与建议

（执行过程中遇到的设计偏差 / 妥协项 / 后续优化点登记到这里。每条形如：「Step N 实施时发现 X，原计划是 Y，实际改为 Z，原因 W」。重要妥协同步登记到 [](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)。）

*本节由执行 agent 在每步结束时增量填写。*

## 六、相关文档

- 设计基线：[M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21)
- 架构基线：[技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21)（§九 LangGraph / §十 LLM 客户端 / §十一前端 token 缓冲）
- 公共上下文：[LittleBox · 公共上下文](https://www.notion.so/LittleBox-0151a091547f4684982113e456acd5dd?pvs=21)
- 路线图：[执行规划：17 个里程碑](https://www.notion.so/17-de81294334b947ef8d598245c73832ad?pvs=21)
- 编写指引：[Agent 指引 · 实施计划编写](https://www.notion.so/Agent-8edba833b10344dcbb5feb9193161952?pvs=21)
- 步骤执行 skill：[Step-Execute Skill v1.4 更新稿](https://www.notion.so/Step-Execute-Skill-v1-4-a92066d4fc6f43a8b3cc177c55c1d560?pvs=21)
- 步骤偏差审核：[Agent 指引 · 步骤差异审核](https://www.notion.so/Agent-d2d23aab6c7e44b899783ba60af9e6f0?pvs=21)
- 妥协跟踪：[](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)