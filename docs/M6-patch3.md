# M6-patch3 · 上下文阈值压缩 + Session 日切重构 — 实施计划

# M6-patch3 · 上下文阈值压缩 + Session 日切重构 — 实施计划

<aside>
⚠️

**v2.1 修订提示（2026-05-12 19:12）**：v2.0 中关于 **token 累加器（K 决策）** 与 **阈值副作用（J 决策）** 的设计已被 **Step 11 / L 决策（方案 R · 阻塞压缩 + 存储 C）** 整体推翻。核心改动：

1. commit② 写 LLM `usage` 真值**快照**（不累加，不做三路径 ±=）
2. 阈值命中翻 `sessions.needs_compression=True` 标志（不再以 `log.warning` 作为唯一副作用）
3. 下一轮 user 到达且标志为 True → graph 内**阻塞压缩**（同步 LLM 调用 + SSE `compression_progress` 帧 + 失败走 user 发送失败链路 + 标志保持 True 待重试）
4. summary 存 `messages` 表 `role='summary'`，旧 active 翻 `status='compressed'`，**不**建独立 `rolling_summaries` 表
5. 压缩范围 = session 内全部 active（不保留最近 N 轮原文）；COMPRESSION_PROMPT 仅做对话内容客观摘要

被推翻的小节均加就近红色 / 橙色 callout 标注。详见 **§2 L 决策** + **§4 Step 11**。

</aside>

> 阶段 3 v2.0（执行计划）。微决策点 5.1–5.4 全部封板；v1.0 草案被简化版替换：以 `sessions.context_token_count` 累加器取代「每轮全量重算」，撤销 `compress_context_if_needed` fire-and-forget 调度；session 标题改中文格式；列表过滤今日 + 顶层 `today_session_id`。
> 

## 元信息

- **父里程碑**：M6 · 主对话链路 - 后端核心（HEAD `20bc3577`，414 passed）
- **patch 序号**：M6-patch3（M6-patch1 = 测试隔离纪律加固；M6-patch2 = LLM 抽象重构 + ChatDeepSeek 切换）
- **分支命名**：`feat/m6-patch3-context-threshold-session-daily`
- **基线版本**：main @ `20bc3577f6e3fbad39ae03ee720ee12e994acaf8`（2026-05-09 18:53 +08:00）
- **Step 数**：11（Step 0 不产 commit；10 个生产 commit + 1 个 alembic revision）
- **测试增量目标**：+25~30 用例，总 ≥440 passed
- **关联前置**：M6 基线 §3.1 / §3.5 / §4.4 / §4.5 / §4.7 / §5.6 / §7.2 / §7.6，技术架构记录 §四 / §九 / §十
- **关联后续**：M7（消费 `today_session_id` 顶层字段 + 列表过滤今日 + 中文标题 + 「回到会话」按钮 null 跳 WelcomeShell；前端不持 `lib/sessionPolicy.ts` 副本）、M8（commit② 内联 `log.warning` → ARQ enqueue；`estimate_tokens` 可选换 tiktoken）、M9（家长 `daily_summary_notify_at` + ARQ scheduled cron）、M11（阿里云推送闭环）

## 1. 目标

### 1.1 做什么

1. prompts 单一来源化；`build_system_prompt` 接线 `chat_stream`
2. `build_context` 删 `LIMIT 20`，返回全量 active（不再算 token）

<aside>
🚫

**第 3 / 4 项 v2.0 描述已被 L 决策（Step 11）整体推翻**，保留作历史；最终实施改 LLM `usage` 真值快照 + `needs_compression` 标志 + 阻塞压缩 graph node。

</aside>

1. **`~~sessions.context_token_count INT`** **累加器**：commit① / commit② 同事务内 `+= estimate_tokens(content)`；末行决策矩阵 discarded 路径 `-=`（§4.7）~~
2. **~~commit② 后内联阈值判定**：`if session.context_token_count >= 500_000: log.warning(...)`，O(1) 字段比较，**不 fire-and-forget**~~
3. Session 日切策略（4h 硬切兜底 + 凌晨 [1,4) 空闲 30min 切）+ 单 session 写入约束 + `last_active_at = user_msg.created_at` 纪律
4. 一次清理 `dev_chat` / `stream_chat` / `sse.py` dev 路径残留
5. `list_sessions` 响应顶层附 `today_session_id: UUID | null` + `sessions` 数组过滤今日 logical_day
6. `today_session_title` 返回 `周一 · 5月11日` 中文格式（数组取星期，不依赖 locale）

### 1.2 不做什么

1. 不实装压缩 worker、不写 `rolling_summaries.turn_summaries`（M8）
2. 不换 tiktoken（M8 可选；累加器场景非强需求）
3. 不动 `user_settings.daily_summary_notify_at` schema / migration（M9）
4. 不实装日终专家 cron / 通知调度（M9）
5. 不引入 `messageCount` 预聚合字段（**撤回**；M7 改用 `today_session_id` 顶层判定）
6. 不动 `mobile/*` 任何代码（M7 消费）
7. 不引入独立 `context_compression_events` 表（M8 真触发审计时再加；MVP 无业务场景）

## 2. 决策点封板表（v2.0）

| 编号 | 主题 | 结论 |
| --- | --- | --- |
| A | 列表空 session 字段 | **撤回 messageCount**。改为 `list_sessions` 响应顶层 `today_session_id: UUID | null`  • `sessions` 数组过滤今日。前端 WelcomeShell 触发 = `today_session_id == null` |
| B | `dev_chat` / `stream_chat` 残留 | **收回 patch3**。Step 1 一次清理（不再移交 M7） |
| C | `sse.py` `except A, B, C:` 语法 | 忽略（Python 3.14 PEP 758 支持） |
| D | session 日切策略 | 规则 1（硬切）：`logical_day(last) != logical_day(now)` → 切，`logical_day(ts) = (ts - 4h).date()`，时区 Asia/Shanghai。规则 2（凌晨空闲）：`now.hour ∈ [1,4)` 且 `now - last > 30min` → 切。常量：`SESSION_HARD_BOUNDARY_HOUR=4` / `SESSION_IDLE_WINDOW=(1,4)` / `SESSION_IDLE_THRESHOLD_MINUTES=30` |
| E | 单 session 写入约束 | 仅当日 session 可写。`ChatStreamRequest.session_id` 仅作 hint，后端按 D 强制重判；SSE `session_meta.session_id` 始终为生效 sid，前端不一致时切换 `activeSessionId`。取消跨 session 上下文继承 |
| F | `last_active_at` 写入 | 必须 `= user_msg.created_at`（不是 `now()`），commit① 同事务内同步 |
| G | 新 session 标题格式 | `周一 · 5月11日`（中文星期 + 中文月日）；用 `["周一",...,"周日"]` 数组取星期文本，不依赖 Python `locale`（生产环境 locale 不可靠） |
| H | 日报双机制 | 生成 05:00（`DAILY_SUMMARY_TRIGGER_HOUR=5`，M8 真用）；推送默认 `DEFAULT_DAILY_NOTIFY_TIME=time(8,0)`（patch3 仅常量登记，UI / 调度 M9） |
| I | token 阈值 | `CONTEXT_COMPRESS_THRESHOLD_TOKENS = 500_000`（V4 1M 上下文的 50%，patch3 真实工作阈值，M8 **不**调小） |
| J ~~（已修订）~~ | ~~阈值触发副作用~~ | ~~仅 `log.warning(...)`，不写库；不建独立事件表；不污染 `rolling_summaries`~~ → **L 决策修订**：阈值命中翻 `needs_compression=True` 标志；压缩本体在下一轮 user 到达时阻塞执行（graph node）；`log.warning` 不再是唯一副作用 |
| K ~~（已废）~~ | ~~token 累加策略~~ | ~~新增 `sessions.context_token_count INT NOT NULL DEFAULT 0` 累加器；commit① / commit② 同事务 `+=`；discarded 路径同事务 `-=`；判定改为 commit② 之后内联 `if session.context_token_count >= THRESHOLD: log.warning(...)`~~ → **2026-05-12 19:12 被 L 决策整体推翻**：字段重命名 `context_size_tokens INT NULL`（commit② 写 LLM `usage` 真值快照，非累加）；新增 `needs_compression BOOLEAN`；三路径 ±= 全删；`estimate_tokens` 函数删除 |
| L | 方案 R · 阻塞压缩 + 存储 C | **取代 K + 修订 J**。① **快照字段**：rename `context_token_count` → `context_size_tokens INT NULL`；commit② 后调 `extract_usage(final_chunk)` 写 `session.context_size_tokens = usage["input_tokens"] + usage["output_tokens"]`。② **标志字段**：新增 `needs_compression BOOLEAN NOT NULL DEFAULT FALSE`；commit② 后 `if context_size_tokens >= THRESHOLD: needs_compression = True`。③ **阻塞压缩**：下一轮 user 到达且 `needs_compression=True` → graph 内 `compress_if_needed` node 同步跑压缩 LLM；失败抛 `CompressionError` 走 user 发送失败链路（SSE error 帧 + 标志保持 True，用户重试再压一次）；成功事务：所有 `status='active'` 消息翻 `compressed`  • 新插一条 `role='summary' status='active'` 消息 + `needs_compression=False`。④ **存储 C**：summary 复用 `messages` 表 `role='summary'`，旧 active 翻 `status='compressed'`，**不**建 `rolling_summaries` 表；按 `created_at` 时间序天然正确；多次压缩自然套娃。⑤ **UI 信号**：压缩开始时 SSE 发 `compression_progress` 帧，payload `{"message": "正在为对话腾出更多空间"}`（M7 前端消费）。⑥ **压缩范围**：session 内全部 active（**不保留**最近 N 轮原文）。⑦ **Prompt**：`COMPRESSION_PROMPT_STUB` 占位为「对话内容客观摘要」，不带情绪 / 风险标签（安全审查独立链路）。⑧ **失败语义**：压缩失败 = user 发送失败，无额外 retry counter / cron 兜底（M8 视实际失败率再评估是否引入 `compression_attempts`） |

## 3. 前置条件

- main @ `20bc3577` 已 sync
- backend venv 已激活，依赖与 M6 一致
- `pytest` baseline 414 passed
- `backend/tests/conftest.py` 测试隔离 fixture 就绪（M6-patch1 加固后）

## 4. 执行步骤

### Step 0 · 建分支 + 前置校准（无 commit）

**任务**

- [x]  `git checkout main && git pull`
- [x]  `git checkout -b feat/m6-patch3-context-threshold-session-daily`
- [x]  `pytest` 跑一遍确认 414 passed 基线
- [x]  复读 §2 决策表 + 基线 §3.5 / §4.4-4.7 / §5.6 / §7.2 / §7.6

**验证**

- ✅ 分支已建
- ✅ baseline 414 passed
- ❌ 任一 fail → STOP，排查环境后再开始

### Step 1 · 清理 dev_chat / stream_chat / [sse.py](http://sse.py) dev 路径

**任务**

- [x]  删除 `backend/app/api/sse.py`
- [x]  `backend/app/main.py` 删除 `sse_router` 导入与 `include_router` 注册
- [x]  `rg 'dev_chat|stream_chat' backend/` 全仓清理（含 tests/ 与 inline docstring）
- [x]  删除 `backend/tests/test_dev_chat.py`（若存在）

**验证清单**

- ✅ `rg 'dev_chat|stream_chat' backend/` 无业务命中
- ✅ `rg 'from app.api import sse' backend/` 无命中
- ✅ `pytest` ≥409 passed（baseline 414 减去 sse 相关用例）

**Commit**

`chore: remove dev_chat / stream_chat / sse dev path residue`

### Step 2 · prompts 单一来源重构

**任务**

- [x]  `chat/prompts.py` 文件头加纪律注释（中文）
- [x]  把 `graph.py` / `me.py` / `context.py` 中所有 inline prompt 字面量迁入（闸门 A 裁决：三方文件无字面量，范围净化为 grep 核验、详 [M6-patch3 · 执行偏差记录](https://www.notion.so/M6-patch3-f88e84905ec542bc8ca55218b65fd194?pvs=21)）
- [x]  新增 `COMPRESSION_PROMPT_STUB` + `build_compression_prompt() -> SystemMessage`（M8 用）

**关键代码**

```python
# backend/app/chat/prompts.py
"""所有 LLM prompt 字符串单一来源 = 本文件。
外部仅通过 import 函数 / 常量访问，禁止在其他模块内联 prompt 字面量。"""
from langchain_core.messages import SystemMessage

COMPRESSION_PROMPT_STUB = "TODO(prompts-content): compression instruction"

def build_compression_prompt() -> SystemMessage:
    return SystemMessage(content=COMPRESSION_PROMPT_STUB)
```

**验证清单**

- ✅ `rg -n '"""|"' backend/app/chat/graph.py backend/app/api/me.py backend/app/chat/context.py` 中无 prompt 字面量（仅保留函数 docstring）
- ✅ `pytest` passed

**Commit**

`refactor(chat): consolidate prompt strings into chat/prompts.py`

### Step 3 · alembic revision: `sessions.context_token_count` + `compression.py` 单函数

<aside>
🔁

**Step 3 已实施但部分内容将在 Step 11 重构**：`context_token_count` 字段 → rename 为 `context_size_tokens INT NULL`；`estimate_tokens` 函数 → 删除；`compression.py` docstring → 改「commit② usage 快照 + 阻塞压缩」。Step 3 commit 本身保留作历史。

</aside>

**任务**

- [x]  `alembic revision -m "m6_patch3_session_context_token_count"` 生成新 revision
- [x]  revision 加字段 `sessions.context_token_count INTEGER NOT NULL DEFAULT 0`
- [x]  新建 `backend/app/chat/compression.py`：仅 `estimate_tokens(content: str) -> int` 单函数 + `CONTEXT_COMPRESS_THRESHOLD_TOKENS = 500_000` 常量
- [x]  更新 `backend/app/models/session.py` ORM 加字段

**关键代码**

```python
# backend/alembic/versions/<hash>_m6_patch3_session_context_token_count.py
def upgrade():
    op.add_column(
        "sessions",
        sa.Column("context_token_count", sa.Integer(), nullable=False, server_default="0"),
    )

def downgrade():
    op.drop_column("sessions", "context_token_count")
```

```python
# backend/app/chat/compression.py
"""上下文 token 累加器辅助。

M6-patch3 范围：仅提供 estimate_tokens 单函数 + 阈值常量。
累加器写入 / 阈值触发判定都在 me.py 主对话事务内联完成。
M8 升级：内联 log.warning → arq_pool.enqueue_job("compress_session", sid)；
本文件可保持不变或扩展。
"""

CONTEXT_COMPRESS_THRESHOLD_TOKENS = 500_000  # V4 1M 上下文的 50%

def estimate_tokens(content: str) -> int:
    """中文 1 char = 1 token；ASCII 4 char = 1 token。M8 可换 tiktoken。"""
    cjk = sum(1 for c in content if "\u4e00" <= c <= "\u9fff")
    ascii_chars = len(content) - cjk
    return cjk + (ascii_chars + 3) // 4
```

**验证清单**

- ✅ `alembic upgrade head` 成功
- ✅ `alembic downgrade -1 && alembic upgrade head` 可逆
- ✅ `sessions` 表 query 显示新字段
- ✅ `pytest` passed

**Commit**

`feat(chat): add sessions.context_token_count + estimate_tokens helper`

### Step 4 · `session_policy.py` 新模块（中文日期标题）

**任务**

- [x]  新建 `backend/app/chat/session_policy.py`
- [x]  三常量 + 占位常量 + `logical_day` + `should_switch_session` + `today_session_title`（中文格式）
- [x]  纯函数零副作用，便于单测

**关键代码**

```python
# backend/app/chat/session_policy.py
from __future__ import annotations
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

SESSION_HARD_BOUNDARY_HOUR = 4
SESSION_IDLE_WINDOW = (1, 4)
SESSION_IDLE_THRESHOLD_MINUTES = 30
DAILY_SUMMARY_TRIGGER_HOUR = 5  # M8 / M9 用
DEFAULT_DAILY_NOTIFY_TIME = time(8, 0)  # M9 落 UI

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

def logical_day(ts: datetime) -> date:
    if ts.tzinfo is None:
        raise ValueError("ts must be timezone-aware")
    return (ts.astimezone(SHANGHAI) - timedelta(hours=SESSION_HARD_BOUNDARY_HOUR)).date()

def should_switch_session(last_active_at: datetime | None, now: datetime) -> bool:
    if last_active_at is None:
        return True
    if logical_day(last_active_at) != logical_day(now):
        return True
    now_local = now.astimezone(SHANGHAI)
    if SESSION_IDLE_WINDOW[0] <= now_local.hour < SESSION_IDLE_WINDOW[1]:
        if now - last_active_at > timedelta(minutes=SESSION_IDLE_THRESHOLD_MINUTES):
            return True
    return False

def today_session_title(now: datetime | None = None) -> str:
    """返回 `周一 · 5月11日` 格式。"""
    now = now or datetime.now(SHANGHAI)
    d = logical_day(now)
    return f"{_WEEKDAY_CN[d.weekday()]} · {d.month}月{d.day}日"
```

**签名约定**：`should_switch_session` 接 `datetime | None`，便于单测注入纯值；调用方自己解包 `latest.last_active_at if latest else None`。

**验证清单**

- ✅ 模块文件存在并可 import
- ✅ `today_session_title(datetime(2026,5,11,14,30,tzinfo=SHANGHAI))` 返回 `"周一 · 5月11日"`
- ✅ `pytest` passed（覆盖测试在 Step 9）

**Commit**

`feat(chat): add session_policy module with daily boundary and CN title`

### Step 5 · `context.py` 改全量 active

**任务**

- [x]  删 `LIMIT 20`
- [x]  SELECT 全部 `status='active'` 消息按 `created_at` ASC
- [x]  签名简化为 `async def build_context(sid, db) -> list[BaseMessage]`，**不**返回 token / 阈值
- [x]  token 估算与阈值常量**不**留在本文件（仍在 `compression.py`）

**关键代码**

```python
# backend/app/chat/context.py
async def build_context(sid: UUID, db: AsyncSession) -> list[BaseMessage]:
    rows = await db.execute(
        select(Message)
        .where(Message.session_id == sid, Message.status == "active")
        .order_by(Message.created_at.asc())
    )
    return [_to_lc_message(m) for m in rows.scalars().all()]
```

**验证清单**

- ✅ `rg 'LIMIT 20' backend/` 无命中
- ✅ `rg 'estimate_tokens|CONTEXT_COMPRESS_THRESHOLD' backend/app/chat/context.py` 无命中
- ✅ `pytest` passed

**Commit**

`refactor(chat): build_context returns full active history`

### Step 6 · `me.py` session 归属判定 + `last_active_at` + commit① 累加器

<aside>
🔁

**Step 6 中 commit① 累加器 `+=` 与末行决策矩阵 discarded 路径 `-=` 将在 Step 11 删除**。session 归属判定 + `last_active_at = user_msg.created_at` 部分保留有效。

</aside>

**任务**

- [x]  Row 1 分支前查 `latest_session`（`child_user_id=current.id` 且 `status='active'`，`ORDER BY last_active_at DESC LIMIT 1`）
- [x]  `now = datetime.now(SHANGHAI)`；调 `should_switch_session(latest.last_active_at if latest else None, now)`
- [x]  True → 新建 session（标题 `today_session_title(now)`，`last_active_at=now`，`context_token_count=0`）；False → 复用 `latest.id`
- [x]  commit① 同事务内：
    - `session.last_active_at = user_msg.created_at`（F 决策）
    - `session.context_token_count += estimate_tokens(req.content)`（K 决策）
- [x]  末行决策矩阵「孤儿 human 改内容重发」discarded 路径同事务 `session.context_token_count -= estimate_tokens(orphan.content)`
- [x]  commit② 不更新 `last_active_at`
- [x]  `ChatStreamRequest.session_id` 字段 docstring 补「前端 hint；后端按日切策略强制重判，不一致时通过 SSE `session_meta.session_id` 回灌生效 sid」
- [x]  SSE `session_meta` payload docstring 补「`session_id` 始终为服务端最终生效 sid」
- [x]  删除 `_truncate_title` 函数与 `import re`

**关键代码**

```python
# backend/app/api/me.py — chat_stream Row 1 分支
now = datetime.now(SHANGHAI)
latest = (await db.execute(
    select(Session)
    .where(Session.child_user_id == current.id, Session.status == "active")
    .order_by(Session.last_active_at.desc())
    .limit(1)
)).scalar_one_or_none()

if should_switch_session(latest.last_active_at if latest else None, now):
    session = Session(
        id=uuid4(),
        child_user_id=current.id,
        title=today_session_title(now),
        status="active",
        last_active_at=now,
        context_token_count=0,
    )
    db.add(session)
    sid = session.id
else:
    session = latest
    sid = latest.id

# 末行决策矩阵 — 孤儿 human 改内容重发路径
if orphan_human is not None and req.regenerate_for is None:
    orphan_human.status = "discarded"
    session.context_token_count -= estimate_tokens(orphan_human.content)

# commit① — user 消息落库（同事务内同步 last_active_at + 累加 token）
user_msg = Message(session_id=sid, role="user", content=req.content, status="active")
db.add(user_msg)
await db.flush()  # 拿 created_at
session.last_active_at = user_msg.created_at
session.context_token_count += estimate_tokens(req.content)
await db.commit()
```

**验证清单**

- ✅ `rg '_truncate_title' backend/` 无命中
- ✅ `rg '^import re' backend/app/api/me.py` 无命中
- ✅ commit① 后查 `sessions` 行 `context_token_count` 字段 > 0
- ✅ 孤儿改内容重发路径下，旧 human 状态 discarded + 累加器有 `-=` 修正
- ✅ `pytest` passed

**Commit**

`feat(chat): enforce session boundary, user-msg time, and commit① token accumulator`

### Step 7 · `me.py` 接线 `build_system_prompt` + commit② 累加器 + 内联阈值判定

<aside>
🔁

**Step 7 中 commit② 累加器 `+=` 与内联 `log.warning` 阈值判定将在 Step 11 改写为 LLM `usage` 真值快照 + `needs_compression=True` 标志**。`build_system_prompt` 接线 + `enqueue_audit` 的 `asyncio.create_task` 调用部分保留有效。

</aside>

**任务**

- [x]  查 `ChildProfile` 取 `age` / `gender`
- [x]  `history = await build_context(sid, db)`
- [x]  构造 `initial_state["messages"] = [build_system_prompt(age, gender), *history, HumanMessage(content=req.content)]`
- [x]  commit② ai 消息落库同事务：`session.context_token_count += estimate_tokens(ai_content)`
- [x]  commit② 之后**内联** `if session.context_token_count >= CONTEXT_COMPRESS_THRESHOLD_TOKENS: log.warning("context exceeded threshold", extra={"session_id": str(sid), "token_count": session.context_token_count})`
- [x]  以 `asyncio.create_task(enqueue_audit(sid))` 启动审查（不 await）；**不再** `create_task(compress_context_if_needed(...))`（已撤销）
- [x]  立即 yield end 帧
- [x]  `graph.py` `enqueue_audit` 仍保持 no-op stub（基线 §7.6 M8 替换）
- [x]  （追加）消除 `graph.py::persist_ai_turn` 内 `session.last_active_at = datetime.now(utc)` 覆写（Step 6 A5 跨步 interim 收尾）

**关键代码**

```python
# backend/app/api/me.py — initial_state 构造
child_profile = await db.get(ChildProfile, current.id)
history = await build_context(sid, db)

initial_state = {
    "messages": [
        build_system_prompt(child_profile.age, child_profile.gender),
        *history,
        HumanMessage(content=req.content),
    ],
    # ... 其余字段同 M6
}

# commit② — ai 消息落库 + 累加器
ai_msg = Message(
    session_id=sid, role="ai", content=final_ai_content,
    status="active", finish_reason=finish_reason,
)
db.add(ai_msg)
session.context_token_count += estimate_tokens(final_ai_content)
await db.commit()

# 内联阈值判定（O(1) 字段比较）
if session.context_token_count >= CONTEXT_COMPRESS_THRESHOLD_TOKENS:
    log.warning(
        "context exceeded threshold",
        extra={"session_id": str(sid), "token_count": session.context_token_count},
    )

# 审查并行入口（无 db_factory 依赖）
asyncio.create_task(enqueue_audit(sid))
yield _end_frame(...)
```

**验证清单**

- ✅ SSE 链路中 graph 收到的 `messages[0].type == "system"`
- ✅ `rg 'compress_context_if_needed' backend/` 无命中（函数已撤销）
- ✅ `enqueue_audit` 调用站点为 `asyncio.create_task`（不被 await）
- ✅ commit② 后 `context_token_count` 累加 ai 内容估算值
- ✅ commit② 后阈值命中场景触发 `log.warning` 一次（mock 验证）
- ✅ `pytest` passed

**Commit**

`feat(chat): wire system prompt, commit② accumulator, inline threshold check`

### Step 8 · `list_sessions` 顶层 `today_session_id` + 过滤今日

**任务**

- [x]  `SessionListResponse` schema 加顶层字段 `today_session_id: UUID | None`（**取代** v1.0 草案中的 `current_logical_day` 字符串；同步重命名 `items → sessions`）
- [x]  `/api/me/sessions` GET handler：
    - 查 latest active session：若 `logical_day(latest.last_active_at) == logical_day(now)` → `today_session_id = latest.id` 否则 `null`
    - `sessions` 数组主查询加 `WHERE Session.id != today_sid`（仅当 `today_sid is not None`）
- [x]  OpenAPI schema 由 FastAPI 自动更新（grep today_session_id 命中 2 处）

**关键代码**

```python
# backend/app/schemas/sessions.py
from uuid import UUID

class SessionListResponse(BaseModel):
    sessions: list[SessionListItem]   # 仅历史
    today_session_id: UUID | None     # 顶层今日 sid，前端 WelcomeShell 触发依据
    next_cursor: str | None
```

```python
# backend/app/api/me.py::list_sessions
now = datetime.now(SHANGHAI)
latest = (await db.execute(
    select(Session)
    .where(Session.child_user_id == current.id, Session.status == "active")
    .order_by(Session.last_active_at.desc())
    .limit(1)
)).scalar_one_or_none()
today_sid = (
    latest.id
    if latest and logical_day(latest.last_active_at) == logical_day(now)
    else None
)

q = (
    select(Session)
    .where(Session.child_user_id == current.id, Session.status == "active")
)
if today_sid is not None:
    q = q.where(Session.id != today_sid)
q = q.order_by(Session.last_active_at.desc())  # + keyset cursor

return SessionListResponse(
    sessions=items,
    today_session_id=today_sid,
    next_cursor=next_cursor,
)
```

**验证清单**

- ✅ GET `/api/me/sessions` 响应顶层含 `today_session_id` 字段
- ✅ 今日已存在 session 时 `today_session_id` ≠ null 且 `sessions` 数组**不含**该 sid
- ✅ 今日尚未建 session 时 `today_session_id == null`
- ✅ 跨硬切点（03:59 → 04:00）时同一 session 从「今日」变为「历史」（前后两次调用观察）
- ✅ `pytest` passed

**Commit**

`feat(api): expose today_session_id and filter today from sessions list`

### Step 9 · 测试

**任务**：新增 7 组用例，全部走 `conftest.py` fixture 隔离。

1. **`test_prompts_single_source.py`**
    - Given: chat 模块文件树
    - When: 正则扫 `graph.py` / `me.py` / `context.py` 中长字符串字面量
    - Then: 除函数 docstring 外无 prompt 字面量命中
2. **`test_session_policy.py`**
    
    ```python
    @pytest.mark.parametrize("last_h,last_m,now_h,now_m,expected", [
        (23, 0,  0, 30, False),
        (23, 30, 0, 55, False),
        ( 0, 50, 3, 50, True),
        ( 2, 0,  2, 25, False),
        ( 1, 0,  1, 45, True),
        ( 3, 50, 4,  1, True),
        ( 3, 50, 4, 10, True),
        ( 2, 0, 10,  0, True),
        ( 8, 0, 14,  0, False),
    ])
    def test_should_switch_session(last_h, last_m, now_h, now_m, expected):
        ...
    
    def test_should_switch_session_null_session():
        assert should_switch_session(None, datetime.now(SHANGHAI)) is True
    
    def test_today_session_title_cn_format():
        # 2026-05-11 是周一
        assert today_session_title(
            datetime(2026, 5, 11, 14, 30, tzinfo=SHANGHAI)
        ) == "周一 · 5月11日"
    ```
    
3. **`test_last_active_at_uses_user_msg_time`**（集成）
    - Given: user 消息 A 时间 `03:58` 写入，LLM 完成 `04:03`；模拟 user 消息 B 在 `04:05` 到达
    - When: chat_stream Row 1 分支判定
    - Then: B 命中 D 规则 1（跨硬切点）→ 新建 session，sid_B ≠ sid_A
4. **`test_chat_stream_first_message_creates_session`**（集成）
    - Given: 用户当前无任何 session
    - When: POST `/api/me/chat/stream` 带 `session_id=None`
    - Then: SSE 首帧 `session_meta.session_id` 非空，DB 中新增 1 条 session，标题匹配中文正则 `^周[一二三四五六日] · \d+月\d+日$`
5. **`test_context_token_count_accumulates`**（集成）
    - Given: 一个全新 session，连续两轮对话（user A 30 字 + ai A 200 字 + user B 50 字 + ai B 300 字）
    - When: 每轮 chat_stream 完成
    - Then: `session.context_token_count` 在四个时点分别严格等于各条 content `estimate_tokens` 的累加和
6. **`test_context_token_count_discarded_rollback`**（集成）
    - Given: 孤儿 human 改内容重发场景，旧 human content "我想问 A"，新 human content "我想问 B"
    - When: chat_stream Row 1 末行决策矩阵命中 discarded 分支
    - Then: 旧 human status='discarded' + `session.context_token_count` 已扣减旧 human 估算值；后续累加新 human
7. **`test_list_sessions_today_filter`**（集成）
    - Given: child 当前有 2 条 active session：sid_old（last_active_at=昨日，logical_day=昨日）+ sid_today（last_active_at=今日，logical_day=今日）
    - When: GET `/api/me/sessions`
    - Then: 响应 `today_session_id == sid_today` 且 `sessions` 数组仅含 sid_old
8. **`test_threshold_inline_log_warning`**（mock）
    - Given: 构造一条 session 行 `context_token_count = 499_999`，新增 user msg 估算 token ≥ 1
    - When: chat_stream commit② 完成后
    - Then: `log.warning("context exceeded threshold", ...)` 被调用 1 次；DB 无任何 compression 相关写入

**验证清单**

- ✅ `pytest -k 'patch3 or session_policy or context_token_count or list_sessions_today or prompts_single_source or threshold_inline' -v` ≥30 cases passed
- ✅ 全套 `pytest` ≥440 passed

**Commit**

`test(chat): cover patch3 session boundary, prompts, accumulator, today filter`

### Step 10 · 收尾 + 偏差登记 + CHANGELOG

**任务**

- [x]  全套 `pytest` 跑绿（≥440 passed）
- [x]  偏差档子页已于 Step 1 闸门 A 裁决后提前建立（[M6-patch3 · 执行偏差记录](https://www.notion.so/M6-patch3-f88e84905ec542bc8ca55218b65fd194?pvs=21)），本步仅补登记未回写的尾巴偏差 + 检查完整性
- [x]  在偏差档 / CHANGELOG 中跨里程碑备忘：
    - **M7**：移除「新建 session」UI 入口；列表渲染 = `sessions[]`（仅历史） + 顶层 `today_session_id`；新增「回到会话」长条按钮（点击跳 `todaySessionId`，若 null 跳 WelcomeShell，永不置灰）；消费 `session_meta.session_id` 作为生效 sid；标题中文格式直接渲染；历史 session 视图只读；**前端不持 `lib/sessionPolicy.ts` 副本**
    - **M8**：commit② 后内联 `log.warning(...)` 替换为 `await arq_pool.enqueue_job("compress_session", sid)`；ARQ worker 写 `rolling_summaries.turn_summaries`；`estimate_tokens` 可选换 tiktoken；日终专家 cron 落 `DAILY_SUMMARY_TRIGGER_HOUR=5`
    - **M9**：`user_settings.daily_summary_notify_at: time DEFAULT '08:00'` 落 alembic + 家长设置页 UI（`@react-native-community/datetimepicker` time 模式）+ ARQ scheduled cron 每 5~15min 扫「今日已生成 + 通知时间到 + `sent_at IS NULL`」批量推
    - **M11**：阿里云移动推送接入 `notifications.sent_at` 写入闭环
- [x]  `git log --oneline` 检查 10 个 commit 全 Conventional Commits 小写前缀（实测 13 条：8 生产 + 1 docs + 1 test + 3 fix；base `20bc3577` → HEAD `93a00c2`，偏差登记见偏差档）

**验证清单**

- ✅ `pytest` 全套 ≥440 passed
- ✅ `git log feat/m6-patch3-context-threshold-session-daily --oneline` 共 10 条 commit
- ✅ 偏差档子页已建（即便为空）

**Commit**

`docs(m6-patch3): record changelog and cross-milestone TODOs`

### Step 11 · 方案 R 落地（阻塞压缩 + 存储 C）—— 推翻 K / J 决策

<aside>
⭐

**一步做完压缩本体 + 含最终审核 + 收口**。范围：schema 调整 + `estimate_tokens` 删除 + graph `compress_if_needed` node + `COMPRESSION_PROMPT_STUB` 占位 + `extract_usage` 真路径锁定 + `me.py` 累加器路径全删 + commit② 快照 + SSE `compression_progress` 帧 + 失败链路对齐 user 发送失败 + 测试改造 + 偏差登记 + CHANGELOG + 技术架构落档 + M8 备忘更新 + 4 工作流文档批量回写。

</aside>

**前置（闸门 A 调研，执行 agent 模板 A 必含）**

- [ ]  `rg 'context_token_count' backend/` 全部命中点（字段引用、测试、迁移、注释）
- [ ]  `rg 'estimate_tokens' backend/` 全部命中点（含测试与 docstring）
- [ ]  `rg "role\s*=\s*['\"]" backend/app/models/message.py` 现有 role 枚举值
- [ ]  `rg "status\s*=\s*['\"]" backend/app/models/message.py` 现有 status 枚举值
- [ ]  **langchain-deepseek 源码定位 streaming usage 真路径**：是 `chunk.usage_metadata` / `chunk.additional_kwargs["usage"]` / `chunk.response_metadata["usage"]` 之一；按 finish_reason 同类陷阱处理（红线 #5 证据优先于推导），**先读 `pip site-packages langchain_deepseek/chat_models.py` 源码再动手**
- [ ]  DeepSeek 官方 API 文档确认 streaming 是否需要 `stream_options={"include_usage": True}` 才在末帧返回 usage
- [ ]  「技术架构讨论记录 §13.2」教训复读：`chunk.response_metadata` 顶层属性恒 `{}`，真实路径走 `chunk.additional_kwargs`，**不试错**

**任务**

- [ ]  **alembic 新 revision**（链 `84781fbc465a` → 新 hash，可逆 downgrade）
    - `RENAME COLUMN sessions.context_token_count TO context_size_tokens`
    - `ALTER COLUMN sessions.context_size_tokens DROP NOT NULL`（旧 0 值保留不洗，新逻辑读 0 不会误触发）
    - `ADD COLUMN sessions.needs_compression BOOLEAN NOT NULL DEFAULT FALSE`
    - `messages.role` 枚举扩 `'summary'`
    - `messages.status` 枚举扩 `'compressed'`
- [ ]  **`compression.py` 重构**：删 `estimate_tokens` 函数；保留 `CONTEXT_COMPRESS_THRESHOLD_TOKENS = 500_000`；新增 `COMPRESSION_PROMPT_STUB`（占位文案：「请将以下对话历史压缩为简洁的客观摘要，保留事件、决定、待办与重要细节；不带情绪标签、不做风险评估、不做安全判断」）；新增 `build_compression_prompt(history: list[BaseMessage]) -> list[BaseMessage]`；docstring 改「commit② usage 快照 + 下一轮 user 到达时阻塞压缩」
- [ ]  **`extractors.py` 新增 `extract_usage(chunk) -> dict | None`**：按 `extract_finish_reason` / `extract_reasoning_content` 同范式；注释 verbatim 引用 langchain-deepseek 源码行号锁定真路径；返回字典含 `input_tokens` / `output_tokens` / `total_tokens`
- [ ]  **`models/session.py`**：字段 rename `context_token_count` → `context_size_tokens: Mapped[int | None]` + 新增 `needs_compression: Mapped[bool]`（default False）
- [ ]  **`models/message.py`**：`role` 枚举扩 `'summary'` + `status` 枚举扩 `'compressed'`
- [ ]  **`me.py` chat_stream 累加器路径全删**：commit① 同事务的 `session.context_token_count += estimate_tokens(req.content)` 删；末行决策矩阵 discarded 路径的 `session.context_token_count -= estimate_tokens(orphan.content)` 删
- [ ]  **`me.py` chat_stream commit② 改快照 + 标志**：详见下方关键代码段
- [ ]  **graph 内新 `compress_if_needed` node**：位置在 chat_stream 进入 graph 后、`chat` node 之前的条件路径；详见下方关键代码段
- [ ]  **`context.py::build_context` 无需改动**：仍按 `status='active' ORDER BY created_at ASC`；压缩后 active 集合 = `[summary_msg, current_user_msg]`，时间序天然正确；多轮压缩套娃链路自动收敛
- [ ]  **SSE 协议层加 `compression_progress` 帧类型**：在 SSE schema 定义；仅在 `compress_if_needed` node 内发一次（开始压缩时）；M7 前端消费（§10 跨里程碑备忘）
- [ ]  **测试改造**
    - 删 `test_context_token_count_accumulates`（K 决策）
    - 删 `test_context_token_count_discarded_rollback`（K 决策）
    - 改 `test_threshold_inline_log_warning` → `test_threshold_sets_needs_compression_flag`：mock `extract_usage` 返回 `{"input_tokens": 300_000, "output_tokens": 200_001}`，验证 commit② 后 `session.context_size_tokens == 500_001 and session.needs_compression == True`
    - 新增 `test_context_size_tokens_snapshot_not_accumulate`：mock 两轮 usage 分别 `(300_000, 200_000)` 与 `(400_000, 100_000)`，验证两轮后 `context_size_tokens == 500_000`（**第二轮快照覆盖，不累加**）
    - 新增 `test_compress_node_blocking_on_flag`：构造 `needs_compression=True` + 3 条 active message → user 到达 → 验证 graph 进入 compress 分支 + SSE `compression_progress` 帧发出 + 压缩 LLM 调用 + 完成后 3 条原 active 翻 `compressed` + 新增 1 条 `role='summary' status='active'` 消息 + `needs_compression=False`
    - 新增 `test_compress_failure_aligns_user_send_failure`：mock 压缩 LLM 抛错 → 验证 SSE error 帧（与 user 发送失败同链路）+ `needs_compression` 保持 True + 原 active 消息状态不变
    - 新增 `test_compress_summary_chained`：构造已有一条 `role='summary' status='active'` 消息 + 多条新增 active → 触发压缩 → 验证旧 summary 翻 `compressed` + 新增第二条 summary active + build_context 按时间序仅返回最新 summary + 当前 user
    - spy `extract_usage` / `compress_if_needed` 等用 `mock.patch("app.chat.xxx.fn_name")` 函数本身，**不**直接 mock `asyncio.create_task`（防 finish_reason 假绿教训复发，红线 #5）
    - 所有新增 asyncio 测试文件加 `pytestmark = pytest.mark.asyncio(loop_scope="function")`（红线 #10）
- [ ]  **偏差档登记**「M6-patch3 · 执行偏差记录」追加段：决策时间线（18:21 累加器暴露设计冗余 / 18:31 拍方案 Q / 18:42 不开 patch4 / 18:53 N+1 丢失诊断 / 18:58 拍阻塞 + 不保留最近 / 19:05 失败链路对齐 user 发送 / 19:12 开工）+ 核心洞察（异步 worker 折叠纪律是隐含约定 → 阻塞同事务零并发窗口）+ 存储方案 C（messages 表 role='summary' 复用）
- [ ]  **跨里程碑备忘更新**「M6 · 执行偏差记录」：M8 备忘原「单行替换 log.warning → ARQ enqueue」整条取消（patch3 内已做完压缩本体）；新增「根据 patch3 Step 11 执行的失败率，决定是否引入 `compression_attempts` 计数 + cron 兜底」
- [ ]  **CHANGELOG + 技术架构落档**：「技术架构讨论记录」新增 §十四「阈值压缩触发时机与存储方案」记录方案 R + 存储 C 完整决策；§13.2 教训复用（usage 字段路径同样要靠源码定位，不试错）；patch3 整体 CHANGELOG 追加「Step 11 一步做完压缩本体，K/J 决策被 L 决策推翻」
- [ ]  **4 工作流文档批量回写 18 条红线锚点**：「Step-Execute Skill v1.6 更新稿」/「Agent 指引 · 步骤差异审核」/「Agent 指引 · 实施计划编写」/「M6-patch · 测试隔离纪律加固」按摘要 §7 / §8 清单批量回写（asyncio loop_scope 强制 / verbatim 措辞 / fixture rg / passed+failed 双数字 / 闸门 A rg 路径存在性 / 第二节实现 vs 第六节关注点内部一致性 / §C 中文化 / 模板 A 自调研 / commit 计数偏差预期化 / 第五节未计划变动同步上第一节 / 不下「待确认」结论 / 单 Step 影响性 vs Step 0+末步全量分工 / 「裸删 vs 净损失」pytest 下限锚点 / 「被删函数现存覆盖来源」清单 / 100% 给 migration 不走 autogenerate / 模板 B 第二节贴新增 test 代码 verbatim / 模板 A 自调研 fixture rg 缺失 / 第五节未计划变动须同步上第一节偏差表）

**关键代码**

```python
# backend/app/api/me.py — chat_stream commit② 改快照 + 标志
ai_msg = Message(
    session_id=sid, role="ai", content=final_ai_content,
    status="active", finish_reason=finish_reason,
)
db.add(ai_msg)
usage = extract_usage(final_chunk)
if usage is not None:
    session.context_size_tokens = usage["input_tokens"] + usage["output_tokens"]
    if session.context_size_tokens >= CONTEXT_COMPRESS_THRESHOLD_TOKENS:
        session.needs_compression = True
await db.commit()
# 不再 log.warning；needs_compression 标志本身是下一轮的触发依据
asyncio.create_task(enqueue_audit(sid))
yield _end_frame(...)
```

```python
# backend/app/chat/graph.py — compress_if_needed node（chat node 前的条件路径）
async def compress_if_needed(state: ChatState) -> ChatState:
    if not state.session.needs_compression:
        return state  # 直通 chat node

    # SSE 信号：开始压缩
    await state.emit_sse("compression_progress", {
        "stage": "compressing",
        "message": "正在为对话腾出更多空间",
    })

    # 拉全部 active → 拼压缩 prompt → 阻塞调 LLM
    actives = await load_active_messages(state.sid, state.db)
    prompt = build_compression_prompt(actives)
    try:
        summary = await llm.ainvoke(prompt)  # 阻塞同步
    except Exception as exc:
        # 失败抛 CompressionError，外层 SSE error 链路捕获，对齐 user 发送失败
        # needs_compression 保持 True，下一轮用户重试时再压一次
        raise CompressionError("压缩失败") from exc

    # 成功事务：active → compressed + 新插 summary message + 标志翻 False
    for m in actives:
        m.status = "compressed"
    state.db.add(Message(
        session_id=state.sid, role="summary",
        status="active", content=summary.content,
    ))
    state.session.needs_compression = False
    await state.db.commit()
    return state  # 进 chat node，build_context 自然读到 [summary, current_user]
```

**验证清单**

- ✅ `rg 'context_token_count' backend/app/` 无业务命中（alembic 历史 revision 文件允许保留）
- ✅ `rg 'estimate_tokens' backend/` 无业务命中
- ✅ `rg 'needs_compression' backend/app/` 至少在 `models/session.py` + `me.py` + `graph.py` 三处命中
- ✅ `rg "role\s*=\s*['\"]summary['\"]" backend/` 至少 1 处（压缩 node 内插入语句）
- ✅ `rg "status\s*=\s*['\"]compressed['\"]" backend/` 至少 1 处（压缩 node 内 UPDATE）
- ✅ alembic upgrade / downgrade 可逆，新 revision 链 `84781fbc465a → <new_hash>`
- ✅ `extract_usage` 测试 / 注释 verbatim 引用 langchain-deepseek 源码行号
- ✅ pytest 全套 ≥440 passed（删 2 + 改 1 + 新增 5 = 净增 4 测试；预期约 454 passed）
- ✅ SSE `compression_progress` 帧 schema 定义在 schemas / SSE 协议文档可见
- ✅ `git log feat/m6-patch3-context-threshold-session-daily --oneline` 比 v2.0 多 1 个生产 commit（Step 11 一个 commit；偏差档 / CHANGELOG / 技术架构 / 工作流文档回写为 Notion 操作，不计 git commit）
- ✅ 闸门 B 模板 B 七节齐全，含「第二节贴新增 test 代码 verbatim」+「第五节未计划变动同步上第一节偏差表」

**Commit**

`feat(chat): blocking compression on threshold (scheme R) — replace K/J`

## 5. 验收清单（patch3 整体）

- ✅ `pytest` ≥440 passed
- ✅ `rg 'LIMIT 20|_truncate_title|dev_chat|stream_chat|compress_context_if_needed|current_logical_day' backend/` 无业务命中
- ✅ `rg 'from app.api import sse' backend/` 无命中
- ✅ GET `/api/me/sessions` 响应顶层含 `today_session_id` 字段；今日 session **不**出现在 `sessions` 数组中
- ✅ chat_stream 链路中 graph 收到的 `messages[0].type == "system"`
- ✅ commit① 后 `session.last_active_at == user_msg.created_at`（非 `now()`）

<aside>
🚫

**以下两项 v2.0 验收点已被 L 决策推翻**。Step 11 后改为：① commit② 写 `context_size_tokens = usage["input_tokens"] + usage["output_tokens"]` 真值快照 + ② 阈值命中翻 `needs_compression=True` 标志 + ③ 下一轮阻塞压缩 + ④ 压缩成功后 active → compressed + 新增 `role='summary'` 消息 + 标志翻 False。具体见 §4 Step 11 验证清单。

</aside>

- ~~✅ commit① / commit② / discarded 路径累加器三处变动各自单测命中~~
- ~~✅ commit② 后内联阈值触发 `log.warning`（500_000+ 场景）~~
- ✅ SSE `session_meta.session_id` 始终为服务端最终生效 sid
- ✅ session 标题为 `周X · M月D日` 中文格式（正则 `^周[一二三四五六日] · \d+月\d+日$`）
- ✅ 10 个 commit 全 Conventional Commits
- ✅ 1 个 alembic revision（`sessions.context_token_count` 字段，可逆）

## 6. 关联文档

- M6 实施计划（父）：[M6 · 主对话链路 - 后端核心 — 实施计划 (6/17)](https://www.notion.so/M6-6-17-a36bdd99fc0f445d86623025c330ea0c?pvs=21)
- M6 设计基线：[M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21)
- M6 偏差档：[M6 · 执行偏差记录](https://www.notion.so/M6-ae216175294b41418ad609103ed3c494?pvs=21)
- M6-patch3 偏差档：[M6-patch3 · 执行偏差记录](https://www.notion.so/M6-patch3-f88e84905ec542bc8ca55218b65fd194?pvs=21)（即时回写）
- M7 草案（消费方）：[M7 · 聊天界面前端 — 实施计划前置讨论草案](https://www.notion.so/M7-8e7b332fa7eb4b5a9afd662715aa5bac?pvs=21)
- 编写规范：[Agent 指引 · 实施计划编写](https://www.notion.so/Agent-8edba833b10344dcbb5feb9193161952?pvs=21)
- 技术架构讨论记录：[技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21)（patch3 收口后回写：session 日切策略 + 日报双机制 + 累加器纪律 + §九 图三日终专家触发时间 03:00 → 05:00）
- 公共上下文：[LittleBox · 公共上下文](https://www.notion.so/LittleBox-0151a091547f4684982113e456acd5dd?pvs=21)

## 7. 发现与建议

<aside>
🚫

**「累加器漂移容忍度」整条已被 L 决策作废**。方案 R 下 `context_size_tokens` 由 LLM 真值 `usage` 快照写入，**不存在估算漂移**；`estimate_tokens` 函数在 Step 11 已删除；M8 备忘更新见 Step 11 任务清单（取消「单行替换 log.warning → ARQ enqueue」，改为根据失败率决定是否引入 `compression_attempts` + cron 兜底）。

</aside>

- **~~累加器漂移容忍度**：`estimate_tokens` 是字符估算非真 tokenize，长期 commit① / commit② / discarded 三种路径下可能与真实 token 数量小幅漂移。M8 切 tiktoken 时无需重建历史累加器（误差不影响 500k 大阈值方向）。若想严格化，M8 加 `RECOMPUTE_CONTEXT_TOKENS` 工具脚本扫表重算~~
- **discarded 路径的并发安全**：末行决策矩阵 discarded 分支与 commit① 同事务，无并发问题；同 session 锁 `chat:lock:{sid}` 防同 sid 重入，跨 session 累加互不影响
- **prompts 字面量扫描**：Step 2 / Step 9.1 的「无 prompt 字面量」断言不要误伤函数 docstring；正则 anchor 在赋值语句右侧或函数调用参数位置
- **测试时区敏感**：Step 9.2 / 9.3 所有时间断言必须带 `tzinfo=SHANGHAI`；naive datetime 会被 `logical_day` 直接抛 `ValueError`
- **M8 升级路径稳定**：commit② 后内联 `log.warning(...)` 升级为 ARQ enqueue 时为单行替换；累加器字段 / `estimate_tokens` 函数 / 阈值常量都不需改动 — 前向契约稳定
- **Step 编号变化备忘**：相比 v1.0 草案，Step 3 / 4 / 5 位置调整（v1.0 = session_policy → context → compression；v2.0 = compression+alembic → session_policy → context），原因是 ORM 字段必须先建出来 [me.py](http://me.py) 才能引用

---

*版本：v2.1（Step 11 追加 / K · J 决策被 L 决策推翻）*

*生成时间：2026-05-11T15:35+08:00 / v2.1 修订 2026-05-12T19:12+08:00*

*生成者：NoNo（Notion AI）*

[M6-patch3 · 执行偏差记录](https://www.notion.so/M6-patch3-f88e84905ec542bc8ca55218b65fd194?pvs=21)