# M9 · 三级干预集成 — 实施计划 (9/17)

# 目标

接通三级干预(crisis / redline / guidance)端到端,完成 M9 主体期 **9 组 ~30 条改动 + 11 commit**(含 §H patch0 范式遗漏收口)。M9-patch0 已落地图边界 + Runtime DI 校准(commit `60c772d`),本里程碑在此骨架上接通业务逻辑。

# 非目标

- 真实 prompt 文案(14 个 STUB slot 留 prompt 文案专题)
- 真实通知推送(notifications stub + M10+ 推送)
- patch0 §A.6 双引擎收口 + D-patch0-10 `audit_tools` 命名重构(外置 patch1)
- 第三方红线规则配置 UI(M10+ 家长端)

# 前置条件

- **alembic head**:`docker compose exec api alembic current` 应输出 `412aed826359`(M8-patch0 落地点);如不一致先 `alembic upgrade head`
- **M9-patch0 已 merge 到 main**:`git log --oneline | grep 60c772d` 命中,确认 `build_main_graph()` / `build_audit_graph()` 无参工厂可用(`backend/app/chat/graph.py` + `backend/app/audit/graph.py`)
- **RuntimeResources 已就绪**:`backend/app/runtime.py::build_runtime` 返回的 `RuntimeResources` 含 `arq_pool` + `audit_redis` + `main_graph` + `audit_graph` 单例(供 §H.2 enqueue_audit 复用)
- **容器就绪**:`docker compose up -d` 后 `docker compose exec api python --version` 应输出 Python 3.14.x(PEP 758 except 子句无括号写法合法,参见 [Agent 指引 · 实施计划编写](https://www.notion.so/Agent-8edba833b10344dcbb5feb9193161952?pvs=21) §5.1)
- **测试基线**:`docker compose exec api pytest backend/tests -q` 全绿(无 patch0 遗留 fixture 报错)
- **DeepSeek V4 系列**:settings 现状 `deepseek_model="deepseek-v4-flash"`,本里程碑不切换模型;参见 Agent 指引 §5.2
- **分支状态**:当前仅 main 分支,本里程碑 Step 1 会从 main 拉出新分支 `feat/m9-intervention-integration`

# 基线决策(已锁定)

- **D-A**:单一 PR(20+ 项新功能,无重构混入)
- **D-B**:单一 alembic revision 吞 3 列改动
- **D-C**:`rolling_summaries.crisis_locked` boolean DROP,统一 `crisis_locked_message_id UUID NULL`(空=未锁定,非空=粘性锁定的 ai_[msg.id](http://msg.id))
- **D-D**:`rolling_summaries` + `audit_records` 两表都加 `target_message_id` 锚点
- **D-E**:5 个新 prompt 写 STUB + `TODO(prompts-content)` slot,文案专题外置
- **D-F**:M9 主体期清理 patch0 测试 fixture 过渡层(`_MainGraphCompat` / `_MainGraphProxy` / `lifespan_context = nullcontext`)

继承 M9-patch0 §B 12 项决议(D-patch0-1 至 D-patch0-12)+ M9 前置草案 D1-D19 决策。

# 修改点清单(9 组 ~30 条)

## §0 准备:schema + ORM + Settings

**0.1 alembic 新 revision(基于 head `412aed826359`)**

- `messages` 加 `turn_number INT NOT NULL DEFAULT 0` + backfill SQL:按 `session_id` 分组,human/ai 按 `created_at ASC` 累计编号,human/ai 同轮共享同号;summary/discarded 行不参与编号(保持 0)
- `rolling_summaries` 加 `crisis_locked_message_id UUID NULL` + **DROP** `crisis_locked` boolean
- `audit_records` 加 `target_message_id UUID NULL`
- downgrade:DROP 3 列 + ADD `crisis_locked boolean DEFAULT false`

**0.2 ORM 同步**

- `app/models/chat.py::Message` 加 `turn_number: Mapped[int]`
- `app/models/audit.py::RollingSummary` 删 `crisis_locked`,加 `crisis_locked_message_id: Mapped[uuid.UUID | None]`
- `app/models/audit.py::AuditRecord` 加 `target_message_id: Mapped[uuid.UUID | None]`

**0.3 `app/config.py` Settings 改造**(D2 决议:main / audit / crisis / redline 统一 thinking enabled + effort=max,compression 不动)

- **新增**:
    - `crisis_context_recent_turns: int = 5`(D11,N=5,含触发轮在内最近 N 对 → 2N=10 条)
    - `redline_context_recent_turns: int = 10`(D16)
    - `redline_turn_summaries_window: int = 50`(D17)
    - `main_thinking_enabled: bool = True`
    - `main_reasoning_effort: str = "max"`(与 audit 统一,提升主对话推理深度)
- **删除**:`deepseek_reasoning_effort: str = "high"`(被 `main_reasoning_effort` 语义重命名 + 改值 high→max 取代)
- **保留**:`audit_thinking_enabled=True` / `audit_reasoning_effort="max"` / `compression_thinking_enabled=False`(compression 保持原样)
- [**factory.py](http://factory.py) 同步改**(消除默认参数依赖):
    - `_PROVIDER_REGISTRY["deepseek"]` lambda **显式传** `thinking_enabled=settings.main_thinking_enabled` + `reasoning_effort=settings.main_reasoning_effort`
    - 其他 builder lambda(`audit_deepseek` / `audit_bailian` / `compression_deepseek`)原本已显式传,不动

## §A ai_msg_id 透传链路(D-patch0-1 + D-D)

- **A.1**:`AuditContextSchema` 加 `target_message_id: uuid.UUID`,删原「不引入」注释
- **A.2**:`enqueue_audit(sid, db, turn_number, child_user_id, target_message_id)` 签名扩参(Python 函数签名 4→5 参数,含 db Session);对应 `enqueue_job("run_audit", str(sid), turn_number, str(child_user_id), str(target_message_id))` ARQ job 业务参数 3→4 位;`job_id` 不变(`audit:{sid}:{turn_number}`);**阶段备注**:Step 11 §H.2 再扩 `arq_pool` / `audit_redis` 入参,enqueue_audit 最终签名 5→7 参数(以 Step 11 核心契约为准)
- **A.3**:`run_audit(ctx, sid, turn_number, child_user_id, target_message_id)` 签名扩参,worker 构造 `AuditContextSchema` 时填入
- **A.4**:`me.py` generator 在 `persist_ai_turn` 返回 `ai_msg.id` 后,下传到 `enqueue_audit`(commit② 之后)
- **A.5**:`app/chat/state.py::AuditState` 加 `target_message_id: uuid.UUID | None`(D1 / D14):由 `load_audit_state` 节点填入(Redis ready 信号 → 取 `signals.target_message_id`;PG 兜底 → 取 `rolling_summaries.crisis_locked_message_id`);供 `build_messages_crisis` 节点内现拉 anchor_window 使用(节点现拉 §十四 §四 铁律、不走 State 携带)

## §B commit① 同步写 turn_number + W1 history 装载

- **B.1**:`me.py` chat_stream **commit① human + commit② ai_msg 两处 `turn_number` 写入**:
    - **决策矩阵**(继承 M6-patch3 / patch0 §B.7,本页自包含):
        
        
        | Row | 触发场景 | commit① human 处理 | turn_number 来源 |
        | --- | --- | --- | --- |
        | 1 | 正常新轮(无 orphan / 无 abort) | 新建 human | `session.ai_turn_counter + 1` |
        | 3 | 客户端断流重发(无 orphan) | 新建 human | `session.ai_turn_counter + 1` |
        | 5 | 用户主动 stop 后重发 | 老 human discarded + 新建 human | 新 human `+1`;老 human 原 turn_number 不动 |
        | 6 | 客户端断流重发(有 orphan) | 复用 orphan human,不新建 | 不改 orphan 已有 turn_number |
    - **备注**:Row 2 / Row 4 与 turn_number 无关,本表略;完整 6 行场景见 M6-patch3 决策矩阵
    - **commit② ai_msg 同步写**:`persist_ai_turn` 中新建 `Message(role=ai)` 时填 `turn_number = session.ai_turn_counter + 1`(与本轮 commit① human 同号);commit② 末尾 `session.ai_turn_counter += 1`,下一轮再用新值
    - 落地与 §0.1 backfill SQL「human/ai 同轮共享同号」语义一致
- **B.2**:`app/chat/context.py` 新增 `load_active_history_for_assembly(sid, current_turn, db) -> list[BaseMessage]`(供 main W1 装配链使用):`WHERE status='active' AND turn_number < current_turn ORDER BY created_at ASC`(SQL 层过滤本轮 human,不依赖 Python pop),前缀注入 `rolling_summaries.turn_summaries` 作 SystemMessage 列表
- **B.3**:`build_context` 保留(audit 历史装载 + summary fallback + 既有单测兼容),不再被 `build_messages_main` 调用。**双路职责边界**:
    - `build_context(sid, db) -> ChatContextSchema`:**audit 路径专用**。返回**含本轮 human**(commit① 已落库)、不注入 `turn_summaries` 前缀、保留 M8 期 summary fallback 行为
    - `load_active_history_for_assembly(sid, current_turn, db) -> list[BaseMessage]`:**main W1 wrapper 装配链专用**。返回**不含本轮 human**(SQL `turn_number < current_turn`)、前缀含 `turn_summaries` SystemMessage 列表
    - 建议两者共享底层私有 helper `_load_active_messages(sid, db, *, until_turn: int | None = None)`,避免 SQL 双份维护

## §C Prompt STUB + format_* 函数(D-E)

`app/chat/prompts.py` 加:

- **C.1**:`STUB_CRISIS_SYSTEM_PROMPT` + `build_crisis_system_prompt(age, gender) -> SystemMessage`(5 段结构同 `build_system_prompt`)
- **C.2**:`STUB_REDLINE_SYSTEM_PROMPT` + `build_redline_system_prompt(age, gender) -> SystemMessage`
- **C.3**:`STUB_REENTRY_WRAPPER_CRISIS` + `format_reentry_wrapper_crisis(user_input) -> str`
- **C.4**:`STUB_REENTRY_WRAPPER_REDLINE` + `format_reentry_wrapper_redline(user_input) -> str`
- **C.5**:`STUB_GUIDANCE_WRAPPER` + `format_guidance_wrapper(user_input, guidance) -> str`
- 5 个新增 `TODO(prompts-content)` slot,与现状 9 个 STUB 风格统一
- **14 slot = 现状 9 个 STUB slot(M8 期 system_prompt / audit_prompt 等)+ M9 新增 5 个(C.1-C.5)**,全部留待 prompt 文案专题统一撰写

## §D 装配上下文函数(D11 / D14 / D15 / D16 / D17 / D18)

`app/chat/context.py` 新增:

- **D.1**:`build_crisis_context(sid, db, target_message_id) -> tuple[SystemMessage, list[BaseMessage]]`(D14 / D18),返回 `(anchor_system, after_anchor)` 供 §E.2 解包:
    - 用 `target_message_id` 解锚 `anchor_msg = Message.get(target_message_id)`(crisis_locked 路径同样以 `rolling_summaries.crisis_locked_message_id` 作锚点)
    - **anchor_window**(anchor 及其之前 N 对 = 2N 条):`WHERE session_id = :sid AND created_at <= :anchor_created_at ORDER BY created_at DESC LIMIT :n*2`,`n = settings.crisis_context_recent_turns`(=5);**绕 status 过滤**(D18 铁律:anchor_window 永不参与压缩,拉取时直接拉物理原文段,不被 status='compressed/discarded' 截断);Python `reversed()` 输出正序后,**整段合并封单条 `SystemMessage(anchor_text)`**(D18 锁定;文本格式 `[anchor 窗口]\n{role}: {content}\n...`)
    - **after_anchor**(anchor 之后到当前的所有 active 行):`WHERE session_id = :sid AND created_at > :anchor_created_at AND status='active' ORDER BY created_at ASC`(**不限条数**;crisis_locked 粘性场景可能跨十几轮,但 active 状态保证压缩窗口外的不会进来);保留原 Human/AIMessage role;**首轮 crisis(非粘性) after_anchor 为空**(anchor = 本轮 ai_msg,本轮 human 由 wrapper 注入)
- **D.2**:`build_redline_context(sid, current_turn, db) -> tuple[list[SystemMessage], list[BaseMessage]]`
    - D15:取最近 `settings.redline_turn_summaries_window`(50)条 `turn_summaries` → SystemMessage 列表
    - D16:`load_recent_active_pairs(sid, current_turn, db, n=settings.redline_context_recent_turns)`(10 对)
- **D.3**:`load_recent_active_pairs(sid, current_turn, db, n) -> list[BaseMessage]`:`WHERE turn_number < current_turn AND status='active' ORDER BY turn_number DESC LIMIT n*2`,Python `reversed()` 输出正序

## §E build_messages_ *与 call_*_llm 接通

`app/chat/graph.py`:

- **E.1**:`build_messages_main` 切 W1 wrapper 模式:`[build_system_prompt(age, gender), *load_active_history_for_assembly(sid, turn, db), HumanMessage(content=format_guidance_wrapper(ctx.user_input, audit.guidance or ""))]`;`guidance=None` 时 wrapper 退化为透传(测试断言)
    - **删除现状 `build_messages_main` 中将 `audit.guidance` 拼为 SystemMessage 插入末位 HumanMessage 前的分支**(M8 等价路径,被 W1 wrapper 取代);避免 guidance 被注入两次
    - **wrapper 仅作用于 LLM 输入装配层**:末位 HumanMessage(wrapped 文案)**不回写 `messages` 表**;DB 中 commit① 落库的本轮 human 内容保持 `ctx.user_input` 原文不变
- **E.2**:`build_messages_crisis` 真实现:`[build_crisis_system_prompt, SystemMessage(anchor_text), *after_anchor, HumanMessage(content=format_reentry_wrapper_crisis(ctx.user_input))]`
- **E.3**:`build_messages_redline` 真实现:`[build_redline_system_prompt, *summaries_systems, *recent_pairs, HumanMessage(content=format_reentry_wrapper_redline(ctx.user_input))]`
- **E.4**:`app/chat/factory.py` 加 `build_crisis_llm(settings)` / `build_redline_llm(settings)`,**复用 `audit_{settings.main_provider}` provider 注册名**(沿用 `audit_thinking_enabled=True` + `audit_reasoning_effort=max`,但不绑 audit_tools);不新增 settings 字段,跟随 audit_* 配置自动联动(D2 决议落地)
    - 实现:`return build_provider_llm(f"audit_{settings.main_provider}", settings)`,fallback chain 不启用(与 audit 一致)
- **E.5**:`call_crisis_llm` / `call_redline_llm` 真接入,取消 `await call_main_llm(...)` 委派,流式逻辑复制 `call_main_llm` 不变
- **E.6**:`load_audit_state` PG 兜底(D1):6 分支降级前查 `rolling_summaries.crisis_locked_message_id`,非 NULL → `crisis_locked=True` + `target_message_id=该 UUID`(供 §D.1 build_crisis_context 解锚);其它信号 False / None

## §F write_audit_results 写 crisis_locked + target_message_id + notifications stub

`app/audit/writers.py`:

- **F.1**:签名加 `target_message_id: uuid.UUID` 参数
- **F.2**:写 AuditRecord 时填 `target_message_id`
- **F.3**:`rolling_summaries` upsert:`rs.crisis_locked_message_id = rs.crisis_locked_message_id or (target_message_id if structured_output.crisis_detected else None)` 短路保留旧值,粘性
- **F.4**:`crisis_detected` 或 `redline_triggered` 命中时 INSERT `notifications` 行 stub,实际推送 M10+

## §H M9-patch0 范式遗漏收口(§十四 三方范式整合 对照)

*执行顺序:Step 10=§G(测试)→ Step 11=§H(本节);本节修改会触发 Step 10 测试 fixture 中 initial_state 的同步清理,故执行时置于 §G 之后*

**对照** [§十四 · 三方范式整合 · FastAPI + ARQ + LangGraph](https://www.notion.so/FastAPI-ARQ-LangGraph-31aeae0f24e54d7396172de2ecbabf60?pvs=21) §六「与现状差异 8 处」,M9-patch0 已收口 8 处主条目,本里程碑顺手清理 2 处遗留(M9-patch0 未发现,实际范式遗漏):

- **H.1 `MainDialogueState` 删除 3 个运行时不变量字段**(对照 §十四 §3.4 终态):
    - `session_id: str` → 已存在于 `ChatContextSchema.session_id`,State 内为 dead field
    - `child_user_id: str` → 已存在于 `ChatContextSchema.child_user_id`,State 内为 dead field
    - `provider: str` → `call_main_llm` 已从 `ctx.settings.main_provider` 取,State 内为 dead field
    - 同步修订 `api/me.py::chat_stream` 构造 `initial_state` 处,删除 这 3 个字段的填充
    - 会影响现有测试 fixture(`tests/chat/` 下所有 initial_state 构造),需同 commit 修订
- **H.2 `enqueue_audit` 改用 `RuntimeResources.arq_pool` 单例**(对照 §十四 §2.2 + §2.4):
    - 现状每次 `create_pool(RedisSettings(host, port, password, database))` 新建 + 用后 `close()` / `disconnect()`,违反「arq_pool 启动期一次构建」铁律(原注释标 M14 优化,实际 §十四 已明文要求)
    - 签名加 `arq_pool: ArqRedis` 入参,从 `me.py` generator 持有的 `res.arq_pool` 传入
    - `set_pending` 用的 `redis` 客户端同样改用 `RuntimeResources.audit_redis`(同一条遗漏)
    - 删除 enqueue_audit 内部 `_build_arq_redis_url()` / `create_pool(...)` / `aclose()` / `disconnect()` 一整段

**范式偏离备查(本期不收口,标 patch1+ follow-up)**:

- `audit/graph.py::AuditGraphState` 内 `sid: str` + `turn_number: int` + `child_profile: dict | None` 为 per-run 不变量,按 §十四 §3.4 终态应入 `AuditContextSchema`(现状从 `worker.py::initial_state` 填入 state);**功能等价但范式偏离**,patch1 收口

## §G 测试 + patch0 过渡层清理(D-F)

**测试隔离纪律**(对照 [Agent 指引 · 实施计划编写](https://www.notion.so/Agent-8edba833b10344dcbb5feb9193161952?pvs=21) §6 + [M6-patch · 测试隔离纪律加固](https://www.notion.so/M6-patch-0636f26e98f94916858983c30fdad01d?pvs=21)):

- 所有涉及 DB / Redis 的测试**必须**通过 `backend/tests/conftest.py` 的 fixture 进入:`api_client` / `db_session` / fakeredis / `dependency_overrides`
- 禁止 subprocess 跑 `app.scripts.*` 连真实库 / httpx 直连 `localhost:8000` / `redis.Redis(...)` 显式连真实 host / `from app.config import settings` 后用 `settings.database_url` 自建 engine / `flushdb()` / `flushall()`
- 测试函数级 docstring 用 **Given/When/Then** 三段式
- 删 `lifespan_context = nullcontext` 后,`app.state.resources` mock 需走 RuntimeResources 工厂 + fixture 单例(`db_engine` + `audit_redis` 走 fixture,`arq_pool` 用 `AsyncMock`,`main_graph` / `audit_graph` 用真实无参工厂构造)
- **G.1**:`tests/chat/` 5 path 测试:main / crisis(lock 触发)/ crisis(lock 恢复仍 crisis)/ redline / guidance
- **G.2**:`tests/audit/` `write_audit_results` 写 `crisis_locked_message_id` + 短路保留 + `target_message_id` 透传
- **G.3**:删除 `_MainGraphCompat` / `_MainGraphProxy` 测试 fixture 过渡层(D-patch0-6 收尾)
- **G.4**:`lifespan_context = nullcontext` 改正常 lifespan + `app.state.resources` mock(走 RuntimeResources 工厂 + fixture 单例)

# 执行步骤

按 11 个 Step + Step 0(建分支)展开。每步含**任务 checkbox / 核心契约 / 验证清单 / commit message** 四段。

## Step 0 · 建分支 + 前置自检(无 commit)

**任务**:

- [ ]  `git checkout main && git pull origin main`
- [ ]  `git checkout -b feat/m9-intervention-integration`
- [ ]  容器自检:`docker compose exec api alembic current` 输出 `412aed826359`
- [ ]  测试基线:`docker compose exec api pytest backend/tests -q` 全绿

**验证**:

- [ ]  `git branch --show-current` == `feat/m9-intervention-integration`
- [ ]  前置条件全部满足(见 # 前置条件 章节)

## Step 1 · alembic + ORM + Settings + factory 改造(§0)

**任务**:

- [ ]  新建 alembic revision(基于 head `412aed826359`),按 §0.1 三表改动
- [ ]  backfill SQL 实现 turn_number 编号(按 session_id 分组 + created_at ASC + human/ai 同轮共享同号)
- [ ]  ORM 同步 §0.2(Message / RollingSummary / AuditRecord)
- [ ]  Settings 改造 §0.3(新增 5 字段 + 删 `deepseek_reasoning_effort`)
- [ ]  `factory.py::_PROVIDER_REGISTRY["deepseek"]` lambda 显式传 `main_thinking_enabled` + `main_reasoning_effort`

**核心契约**(alembic revision skeleton):

```python
def upgrade():
    op.add_column("messages", sa.Column("turn_number", sa.Integer, nullable=False, server_default="0"))
    op.add_column("rolling_summaries", sa.Column("crisis_locked_message_id", postgresql.UUID, nullable=True))
    op.drop_column("rolling_summaries", "crisis_locked")
    op.add_column("audit_records", sa.Column("target_message_id", postgresql.UUID, nullable=True))
    # backfill turn_number(同轮共享同号;summary/discarded 不参与)
    op.execute("""
        WITH numbered AS (
          SELECT id, row_number() OVER (PARTITION BY session_id, role ORDER BY created_at) AS rn
          FROM messages WHERE status='active' AND role IN ('human','ai')
        )
        UPDATE messages m SET turn_number = n.rn FROM numbered n WHERE m.id = n.id;
    """)
```

**验证**:

- [ ]  `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` 三循环无错
- [ ]  backfill 在含 orphan / discarded / summary 行的 fixture session 上编号正确(human/ai 同轮同号、summary/discarded turn_number=0)
- [ ]  `docker compose exec api python -c "from app.config import settings; print(settings.main_reasoning_effort)"` 输出 `max`
- [ ]  grep `deepseek_reasoning_effort` 仓库内无剩余引用

**commit**:`feat(m9): alembic + orm + settings + factory explicit llm config`

## Step 2 · target_message_id 透传链路(§A)

**任务**:

- [ ]  `app/audit/context_schema.py::AuditContextSchema` 加 `target_message_id: uuid.UUID`
- [ ]  `app/chat/graph.py::enqueue_audit` 签名扩参(同 Step 11 一起改,本 Step 仅扩参,arq_pool 迁移放 Step 11)
- [ ]  `app/audit/worker.py::run_audit` 签名扩参,构造 ctx 时填入
- [ ]  `app/api/me.py::chat_stream` generator 在 commit② 后传 `ai_msg.id` 给 `enqueue_audit`
- [ ]  `app/chat/state.py::AuditState` 加 `target_message_id: uuid.UUID | None`

**核心契约**:

```python
# audit/context_schema.py
class AuditContextSchema:
    session_id: uuid.UUID
    child_user_id: uuid.UUID
    target_message_id: uuid.UUID  # M9 新增,必非空
    max_iter: int
    settings: Settings
    db_session_factory: async_sessionmaker
    audit_redis: Redis

# chat/state.py
class AuditState(TypedDict):
    crisis_locked: bool
    crisis_detected: bool
    redline_triggered: bool
    guidance: str | None
    target_message_id: uuid.UUID | None  # M9 新增,可空(main 图路径)
```

**验证**:

- [ ]  [me.py](http://me.py) → enqueue_job 参数列表含 target_message_id(用 AsyncMock 断言)
- [ ]  worker 单测:run_audit 构造的 [ctx.target](http://ctx.target)_message_id 与入参一致
- [ ]  AuditState 新增字段后现有测试全绿(对 .get("target_message_id") 兜底 None 友好)

**commit**:`feat(m9): propagate target_message_id through audit pipeline`

## Step 3 · commit① human + commit② ai_msg turn_number 双写 + W1 history loader(§B)

**任务**:

- [ ]  `me.py::chat_stream` commit① 新建 / 复用 human 按决策矩阵 Row 1/3/5/6 设 `turn_number`
- [ ]  `me.py::persist_ai_turn` commit② 新建 ai_msg 填 `turn_number = session.ai_turn_counter + 1`(同本轮 human 同号),末尾 `session.ai_turn_counter += 1`
- [ ]  `app/chat/context.py` 新增 `load_active_history_for_assembly(sid, current_turn, db)` + 私有 helper `_load_active_messages`
- [ ]  `build_context` audit 路径保留(仅重构依赖到 `_load_active_messages`)

**核心契约**:

```python
async def _load_active_messages(sid, db, *, until_turn=None):
    stmt = select(Message).where(Message.session_id == sid, Message.status == 'active')
    if until_turn is not None:
        stmt = stmt.where(Message.turn_number < until_turn)
    return list((await db.execute(stmt.order_by(Message.created_at.asc()))).scalars())

async def load_active_history_for_assembly(sid, current_turn, db):
    rs = await db.scalar(select(RollingSummary).where(RollingSummary.session_id == sid))
    summaries = [SystemMessage(content=s) for s in (rs.turn_summaries or [])] if rs else []
    rows = await _load_active_messages(sid, db, until_turn=current_turn)
    return [*summaries, *[_to_lc(m) for m in rows]]
```

**验证**:

- [ ]  commit① / commit② 双写:新轮 human 与 ai_msg.turn_number 相等
- [ ]  decision matrix Row 5:老 human discarded 后 turn_number 不动、新 human +1、ai_msg 同号
- [ ]  `load_active_history_for_assembly` 不含本轮 human(SQL `turn_number < current_turn`)
- [ ]  `build_context` audit 路径单测仍过

**commit**:`feat(m9): write turn_number in both commits + w1 history loader`

## Step 4 · Prompt STUB + format_* wrapper(§C)

**任务**:

- [ ]  `app/chat/prompts.py` 加 5 个 STUB 常量 + 5 个构造函数(C.1-C.5)
- [ ]  所有新增带 `TODO(prompts-content)` 标记
- [ ]  `format_guidance_wrapper` guidance 为空时透传 user_input

**核心契约**:

```python
STUB_GUIDANCE_WRAPPER = "TODO(prompts-content): 引导 wrapper {user_input} / {guidance}"

def format_guidance_wrapper(user_input, guidance):
    if not guidance:
        return user_input  # 透传,main 主路径 guidance=None 等价
    return STUB_GUIDANCE_WRAPPER.format(user_input=user_input, guidance=guidance)
```

**验证**:

- [ ]  grep `TODO(prompts-content)` 命中 14 处(9 现有 + 5 新增)
- [ ]  `format_guidance_wrapper("hi", None) == "hi"`
- [ ]  `format_guidance_wrapper("hi", "be safe") != "hi"` 且含 STUB 标记

**commit**:`feat(m9): add crisis/redline prompts stub + wrapper formatters`

## Step 5 · context 装配函数(§D)

**任务**:

- [ ]  `build_crisis_context(sid, db, target_message_id) -> tuple[SystemMessage, list[BaseMessage]]` 实现双段
- [ ]  `build_redline_context(sid, current_turn, db) -> tuple[list[SystemMessage], list[BaseMessage]]` 实现
- [ ]  `load_recent_active_pairs(sid, current_turn, db, n)` 实现

**核心契约**:

```python
async def build_crisis_context(sid, db, target_message_id):
    anchor = await db.scalar(select(Message).where(Message.id == target_message_id))
    # anchor_window: 绕 status,LIMIT 2N
    aw_rows = list(reversed(list((await db.execute(
        select(Message)
        .where(Message.session_id == sid, Message.created_at <= anchor.created_at)
        .order_by(Message.created_at.desc())
        .limit(settings.crisis_context_recent_turns * 2)
    )).scalars())))
    anchor_text = "\n".join(f"{m.role}: {m.content}" for m in aw_rows)
    anchor_system = SystemMessage(content=f"[anchor 窗口]\n{anchor_text}")
    # after_anchor: 仅 active,不限条数
    after_rows = (await db.execute(
        select(Message)
        .where(Message.session_id == sid, Message.created_at > anchor.created_at, Message.status == 'active')
        .order_by(Message.created_at.asc())
    )).scalars()
    return anchor_system, [_to_lc(m) for m in after_rows]
```

**验证**:

- [ ]  anchor_window 含 2N 条且绕 status(含 discarded 行)
- [ ]  after_anchor 仅 active 且 anchor 之后
- [ ]  首轮 crisis(非粘性):anchor = 本轮 ai_msg → after_anchor 为空
- [ ]  crisis_locked 粘性 + 跨 12 轮:after_anchor 含全部 12 轮 active

**commit**:`feat(m9): crisis/redline context assemblers + recent pairs loader`

## Step 6 · build_messages_main 切 W1 wrapper(§E.1)

**任务**:

- [ ]  `app/chat/graph.py::build_messages_main` 切 wrapper 模式
- [ ]  删除原 `audit.guidance` 拼 SystemMessage 插入末位 HumanMessage 前的分支
- [ ]  装配链改调 `load_active_history_for_assembly`,不再调 `build_context`

**核心契约**:

```python
async def build_messages_main(state, runtime):
    ctx = runtime.context
    audit = state["audit_state"]
    async with ctx.db_session_factory() as db:
        history = await load_active_history_for_assembly(
            ctx.session_id, state["turn_number"], db
        )
    return {"messages": [
        build_system_prompt(ctx.age, ctx.gender),
        *history,
        HumanMessage(content=format_guidance_wrapper(ctx.user_input, audit.get("guidance"))),
    ]}
```

**验证**:

- [ ]  guidance=None:末位 HumanMessage.content == ctx.user_input(透传)
- [ ]  guidance 非空:末位 HumanMessage.content 含 STUB_GUIDANCE_WRAPPER 标记
- [ ]  DB messages 表 commit① human.content 仍为原文(未被 wrapper 污染)
- [ ]  M8 期 `build_context` 单测仍过

**commit**:`feat(m9): switch build_messages_main to w1 wrapper mode`

## Step 7 · crisis/redline build_messages + call_llm 真接入(§E.2-E.5)

**任务**:

- [ ]  `build_messages_crisis` / `build_messages_redline` 改真实现
- [ ]  `factory.py::build_crisis_llm` / `build_redline_llm` 复用 `audit_{settings.main_provider}` provider
- [ ]  `call_crisis_llm` / `call_redline_llm` 取消 `await call_main_llm(...)` 委派,复制流式逻辑

**核心契约**:

```python
def build_crisis_llm(settings):
    return build_provider_llm(f"audit_{settings.main_provider}", settings)

async def build_messages_crisis(state, runtime):
    ctx = runtime.context
    audit = state["audit_state"]
    async with ctx.db_session_factory() as db:
        anchor_system, after_anchor = await build_crisis_context(
            ctx.session_id, db, audit["target_message_id"]
        )
    return {"messages": [
        build_crisis_system_prompt(ctx.age, ctx.gender),
        anchor_system,
        *after_anchor,
        HumanMessage(content=format_reentry_wrapper_crisis(ctx.user_input)),
    ]}
```

**验证**:

- [ ]  crisis / redline LLM 实例配置与 audit_deepseek 一致(thinking=enabled + effort=max)
- [ ]  crisis / redline LLM 不绑 audit_tools(检查 .tools 属性)
- [ ]  crisis / redline 流式 token 正常输出 + finish 信号

**commit**:`feat(m9): wire crisis/redline build_messages + call_llm with audit provider`

## Step 8 · load_audit_state PG 兜底(§E.6)

**任务**:

- [ ]  `load_audit_state` 6 分支降级前查 `rolling_summaries.crisis_locked_message_id`
- [ ]  PG 兜底命中时填 `crisis_locked=True` + `target_message_id=该值` + 其它信号 False/None

**核心契约**:

```python
async def load_audit_state(state, runtime):
    ctx = runtime.context
    signals = await load_redis_signals(ctx.audit_redis, ctx.session_id, state["turn_number"])
    if signals is not None:
        return {"audit_state": {**signals.to_dict(), "target_message_id": signals.target_message_id}}
    # PG 兜底
    async with ctx.db_session_factory() as db:
        rs = await db.scalar(select(RollingSummary).where(RollingSummary.session_id == ctx.session_id))
    locked_id = rs.crisis_locked_message_id if rs else None
    return {"audit_state": {
        "crisis_locked": locked_id is not None,
        "crisis_detected": False,
        "redline_triggered": False,
        "guidance": None,
        "target_message_id": locked_id,
    }}
```

**验证**:

- [ ]  Redis miss + crisis_locked_message_id 非空 → 主图路由到 crisis
- [ ]  Redis miss + crisis_locked_message_id NULL → 主图路由到 main(降级)
- [ ]  Redis hit → 不查 PG(spy db_session_[factory.call](http://factory.call)_count == 0)

**commit**:`feat(m9): pg fallback for crisis_locked in load_audit_state`

## Step 9 · writers 写 crisis_locked + target_message_id + notify stub(§F)

**任务**:

- [ ]  `write_audit_results` 签名扩 `target_message_id`
- [ ]  AuditRecord 写 target_message_id
- [ ]  rolling_summaries upsert:`crisis_locked_message_id` 短路保留(粘性)
- [ ]  crisis_detected / redline_triggered 命中 → INSERT notifications stub
- [ ]  `audit/graph.py::write_results` 节点调用处传 `ctx.target_message_id`

**核心契约**:

```python
async def write_audit_results(db, session_id, turn_number, structured_output,
                              session_notes_final, turn_summary, target_message_id):
    db.add(AuditRecord(
        session_id=session_id, turn_number=turn_number,
        target_message_id=target_message_id, ...
    ))
    rs = await db.scalar(select(RollingSummary).where(RollingSummary.session_id == session_id))
    if rs is None:
        rs = RollingSummary(session_id=session_id); db.add(rs)
    # 短路保留:已锁定不覆盖
    if rs.crisis_locked_message_id is None and structured_output.crisis_detected:
        rs.crisis_locked_message_id = target_message_id
    if structured_output.crisis_detected or structured_output.redline_triggered:
        db.add(Notification(session_id=session_id, ai_msg_id=target_message_id, ...))
    await db.commit()
```

**验证**:

- [ ]  crisis 命中 → `crisis_locked_message_id == target_message_id`
- [ ]  后续轮 crisis_detected=False 但 crisis_locked_message_id 保留旧值
- [ ]  notifications 行成功 INSERT(stub)

**commit**:`feat(m9): persist crisis_locked + target_message_id + notify stub`

## Step 10 · 5-path 测试 + 清理 patch0 fixture(§G)

**任务**:

- [ ]  `tests/chat/` 5 path 测试用例(main / crisis 触发 / crisis lock 粘性 / redline / guidance)
- [ ]  `tests/audit/` writers 测试(short-circuit + target_message_id 透传)
- [ ]  删除 `_MainGraphCompat` / `_MainGraphProxy` / `lifespan_context = nullcontext` 过渡层
- [ ]  `app.state.resources` mock 走 RuntimeResources 工厂 + fixture 单例(见 §G 测试隔离纪律)
- [ ]  新增测试 docstring 都用 Given/When/Then

**验证**:

- [ ]  `pytest backend/tests` 全绿
- [ ]  grep `nullcontext` 在 `tests/` 下不再命中
- [ ]  grep `_MainGraphCompat` / `_MainGraphProxy` 不再命中
- [ ]  grep `Given:` 在本次新增测试函数 docstring 中命中 ≥ 5 处

**commit**:`test(m9): 5-path coverage + drop patch0 transition fixtures`

## Step 11 · patch0 范式遗漏收口(§H.1 + §H.2)

**任务**:

- [ ]  `app/chat/state.py::MainDialogueState` 删 `session_id` / `child_user_id` / `provider` 3 字段
- [ ]  `app/api/me.py::chat_stream` initial_state 构造同步删 3 个字段填充
- [ ]  `app/chat/graph.py::enqueue_audit` 改用 `arq_pool: ArqRedis` + `audit_redis: Redis` 入参,删 `create_pool(...)` / `_build_arq_redis_url()` / `aclose()` / `disconnect()`
- [ ]  `me.py` generator 调用处传 `res.arq_pool` + `res.audit_redis`(`res: RuntimeResources = request.app.state.resources`)
- [ ]  Step 10 新增测试同步清理 initial_state 构造(删除 3 字段填充)

**核心契约**:

```python
# 改后签名
async def enqueue_audit(
    arq_pool: ArqRedis,
    audit_redis: Redis,
    sid: uuid.UUID,
    db: AsyncSession,
    turn_number: int,
    child_user_id: uuid.UUID,
    target_message_id: uuid.UUID,
) -> None:
    manager = AuditSignalsManager(audit_redis, ttl=settings.audit_redis_ttl_seconds)
    await manager.set_pending(sid, turn_number)
    await arq_pool.enqueue_job(
        "run_audit", str(sid), turn_number, str(child_user_id), str(target_message_id),
        _job_id=f"audit:{sid}:{turn_number}",
    )
```

**验证**:

- [ ]  grep `create_pool` 在 `chat/graph.py` 不再命中
- [ ]  grep `_build_arq_redis_url` 在 `chat/graph.py` 不再命中
- [ ]  grep `MainDialogueState` 中 `session_id` / `child_user_id` / `provider` 不再命中
- [ ]  全链路集成测试无 KeyError(session_id / child_user_id / provider 引用已清理)
- [ ]  审查任务正常 enqueue(集成测试断言 ARQ 队列收到 job)

**commit**:`refactor(m9): patch0 paradigm cleanup - state purge + arq_pool singleton`

# PR 提交

- 11 个 commit 全部完成后:`git push origin feat/m9-intervention-integration`
- 提 PR 到 `main`,说明栏链接本计划页 + [M9 · 执行偏差记录](https://www.notion.so/M9-e60b2299be524ec990649670657e904a?pvs=21)
- PR 标题:`feat(m9): three-tier intervention integration + patch0 paradigm cleanup`

# 验收清单

- [ ]  alembic upgrade head + downgrade 1 step + upgrade head 三次循环无错误
- [ ]  backfill SQL 在含 discarded / summary 行的 fixture 上 `turn_number` 编号正确
- [ ]  5 path 集成测试全过(main / crisis 触发 / crisis lock 粘性 / redline / guidance)
- [ ]  `crisis_locked_message_id` 短路语义测试:命中 crisis 后非空;后续轮 `crisis_detected=False` 但旧值保留
- [ ]  `target_message_id` 透传链路 e2e:`me.py` → `enqueue_audit` → `run_audit` → `AuditContextSchema` → `write_audit_results` 端到端断言
- [ ]  `load_audit_state` PG 兜底:Redis miss + `rolling_summaries.crisis_locked_message_id` 非空 → 主图路由到 crisis
- [ ]  W1 wrapper 切换后,`build_messages_main` 末位 HumanMessage 不与 `load_active_history_for_assembly` 末位重复
- [ ]  notifications stub:`crisis_detected` 命中时新增 `notifications` 行
- [ ]  patch0 测试 fixture 过渡层全部移除(`_MainGraphCompat` / `_MainGraphProxy` / `nullcontext` `lifespan_context`)
- [ ]  M8 期 `build_context` 单元测试仍过(audit 路径 + summary fallback)
- [ ]  §H.1 `MainDialogueState` 删除 session_id / child_user_id / provider 后,`me.py` initial_state 同步更新,全链路测试无 KeyError
- [ ]  §H.2 `enqueue_audit` 改用 `arq_pool` 单例后,审查任务正常 enqueue;grep `create_pool` 在 chat/[graph.py](http://graph.py) 中不再出现
- [ ]  `main_reasoning_effort` / `main_thinking_enabled` 改造后,grep `thinking_enabled=True`(或 = audit_thinking_enabled)仅出现在 builder lambda 显式传参处,不靠 `_build_chat_deepseek` 默认值
- [ ]  `build_crisis_llm` / `build_redline_llm` 返回的 LLM 实例配置与 `audit_deepseek` 一致(thinking=enabled + effort=max + 不绑 audit_tools)

# 风险与回滚

**风险 1 · backfill SQL 误编号**

- 缓解:在 fixture session(含 orphan / discarded / summary 混合)上单测 backfill 输出
- 回滚:alembic downgrade -1 + ORM 回退

**风险 2 · W1 切换破坏 M6 / M8 既有测试**

- 缓解:`build_context` 保留不删,audit 路径继续走旧接口;新加 `load_active_history_for_assembly` 是独立函数
- 回滚:`build_messages_main` 单 commit 切换(#6),独立回滚不影响 §A-D

**风险 3 · target_message_id 透传链路断点**

- 缓解:A.4 `me.py` generator 修改与 `persist_ai_turn` 同 commit;签名扩参错误会编译期失败
- 回滚:§A 整组单 commit 回滚

**风险 4 · DROP crisis_locked boolean 列后丢失历史信号**

- 缓解:alembic upgrade SQL 先验证 `SELECT COUNT(*) WHERE crisis_locked=true = 0`(生产无脏数据)
- 回滚:downgrade ADD COLUMN `crisis_locked boolean DEFAULT false`(语义损失,但表结构兼容)

# 发现与建议

**本次规划过程发现 + 沉淀**:

- **§十四 范式遗漏 2 处**(M9-patch0 期未发现):`MainDialogueState` 残留 session_id / child_user_id / provider 3 个 dead field · `enqueue_audit` 未复用 `RuntimeResources.arq_pool` 单例 —— 已纳入 §H 主体期收口
- **§十四 范式偏离 1 处**(`AuditGraphState.sid` / `turn_number` / `child_profile`):功能等价但该入 ctx,标 patch1 follow-up
- **`target_message_id` 双路填入类型分歧**:audit 图路径(必非空,入 `AuditContextSchema`)vs main 图路径(可空,入 `AuditState`),§A.1 + §A.5 + §F.1 已明文区分类型签名,执行时注意 None 兜底
- **`anchor_window` / `after_anchor` 切分**:前置草案 D14 / D18 仅讨论 anchor_window,after_anchor 在 crisis_locked 粘性路径必需 —— §D.1 已补 SQL 与边界条件;前置草案 D14 应回写「拆两段」说明
- **commit② ai_msg.turn_number 同号写入**:前置草案仅讨论 commit① human.turn_number,ai_msg 同号写入以前隐含未明言 —— §B.1 已补明
- **W1 wrapper 与 DB 落库解耦**:wrapper 仅作 LLM 输入装配,DB 中 ctx.user_input 原文不变 —— §E.1 已明文,避免后续 prompt 文案专题误改

**建议同步动作**(M9 主体期之外):

- patch0 偏差记录页 [M9-patch0 · 执行偏差记录](https://www.notion.so/M9-patch0-35b24688f16645f999a931bed37aa745?pvs=21) 补登 §H.1 / §H.2 两条遗漏(M9 主体期已收口,登记便于审计追溯)
- M9 偏差记录页 [M9 · 执行偏差记录](https://www.notion.so/M9-e60b2299be524ec990649670657e904a?pvs=21) 在主体期实施过程中按 Agent 指引 §4 模板登记
- 前置草案 [M9 · 三级干预集成 — 前置讨论草案](https://www.notion.so/M9-4088f037ad954d37bd1975ddc32f1842?pvs=21) D14 可选补「anchor_window vs after_anchor」拆分说明(后续里程碑以及期间 patch 需复用该结论)

# Follow-up(外置 patch1,M9 收口后)

- D-patch0-10:`audit_tools` 节点函数重命名(候选 `audit_tool_dispatch`)
- D-patch0 §A.6:双引擎收口(`app.db._engine` 单例移除,所有路径走 `rr.db_engine`)
- `AuditGraphState` 净化:`sid` / `turn_number` / `child_profile` 迁 `AuditContextSchema`(§十四 §3.4 终态,patch0 期妥协遗留)
- prompt 文案专题:14 个 STUB slot(9 现有 + 5 新增)真实文案撰写 + 评审

# 参考文档

- M9-patch0 实施计划:[Refactor: M9-patch0 · 图边界 + Runtime DI 校准](https://www.notion.so/Refactor-M9-patch0-Runtime-DI-9aba677f06ca467b805d0bfb466efc47?pvs=21)
- M9-patch0 执行偏差记录:[M9-patch0 · 执行偏差记录](https://www.notion.so/M9-patch0-35b24688f16645f999a931bed37aa745?pvs=21)
- M9 前置讨论草案:[M9 · 三级干预集成 — 前置讨论草案](https://www.notion.so/M9-4088f037ad954d37bd1975ddc32f1842?pvs=21)
- 技术架构讨论记录:[技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21)
- M6–M9 设计基线:[M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21)
- §十四 三方范式整合:[§十四 · 三方范式整合 · FastAPI + ARQ + LangGraph](https://www.notion.so/FastAPI-ARQ-LangGraph-31aeae0f24e54d7396172de2ecbabf60?pvs=21)

[M9 · 执行偏差记录](https://www.notion.so/M9-e60b2299be524ec990649670657e904a?pvs=21)