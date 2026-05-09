# M6 · LLM 抽象重构 + ChatDeepSeek 切换 — 实施计划（M6 收尾补丁）

<aside>
🩹

**M6 收尾补丁 · 独立执行计划**

本页是 [M6 · 主对话链路 - 后端核心 — 实施计划 (6/17)](https://www.notion.so/M6-6-17-a36bdd99fc0f445d86623025c330ea0c?pvs=21) 的收尾补丁，独立交付。**执行节奏：一次验收，不走分步审核**——5 个子步可连续执行 + 独立 commit + 独立单测，闸门 A 在 Step 11.1 启动前一次性审核全包任务清单，闸门 B 在 Step 11.5 收尾时一次性审核整包实装。中间不暂停回 Iver。

执行纪律：[Step-Execute Skill v1.6 更新稿](https://www.notion.so/Step-Execute-Skill-v1-6-a92066d4fc6f43a8b3cc177c55c1d560?pvs=21) + [Agent 指引 · 步骤差异审核](https://www.notion.so/Agent-d2d23aab6c7e44b899783ba60af9e6f0?pvs=21)。

</aside>

## 一、背景与目标

### 1.1 为什么需要这个补丁

M6 主 plan 已 Step 10 收尾（375 passed deterministic），但 Step 2.5（commit `4716973`）将 LLM 客户端切到 `langchain_openai.ChatOpenAI(base_url=...) + with_fallbacks` 后，**主对话链路丢失了 reasoning_content**——`AIMessageChunk.additional_kwargs.reasoning_content` 始终为空，思考内容无法回流给前端 / 入库 / 走 SSE thinking_start/thinking_end 信号。

根因不是 bug，是 `langchain-openai` 的**设计纪律**：

- **证据 1**：`langchain-openai` master 分支 `libs/partners/openai/langchain_openai/chat_models/base.py` docstring 黑字明文：「ChatOpenAI targets official OpenAI API specifications **only**. Non-standard response fields added by third-party providers (e.g., `reasoning_content`, `reasoning_details`) are **not** extracted or preserved... use `ChatDeepSeek` / `ChatOpenRouter`」
- **证据 2**：DeepSeek 官方 API 文档（`api-docs.deepseek.com`）端点列表 = Chat / Completions / Models / Others，**无 `/v1/responses` 端点**；思考模式参数走 `/chat/completions` 的 `thinking={"type":"enabled"}` + `reasoning_effort`
- **证据 3**：阿里云百炼 Responses API 兼容文档（`help.aliyun.com/zh/model-studio/compatibility-with-openai-responses-api`，2026-03-02）仅明确支持「通义千问模型」，**DeepSeek-V4-Flash 走百炼 compat 端是否走 Responses 文档未明示**；旧端点路径仍是 `/compatible-mode/v1/chat/completions`
- **证据 4**：`langchain-openai` 0.3.9 引入 `use_responses_api` / 0.3.24 引入 `BaseChatOpenAI.reasoning` / 1.0.0 默认 `output_version="responses/v1"`，但**这套机制只在 base_url 是真 Responses 端点时生效**；DeepSeek 原生 + 百炼 compat 端都不是，硬开 `use_responses_api=True` 会 405（GitHub Issue #31653 同症状）

四象限结论：DeepSeek 模型 × {原生端 / 百炼 compat 端} × `langchain-openai` ChatOpenAI = **reasoning_content 一定丢**。要拿回必须切 `langchain_deepseek.ChatDeepSeek`（继承 `BaseChatOpenAI` 但**重写了响应解析**保留 reasoning_content，源在 `libs/partners/deepseek/langchain_deepseek/chat_models.py`）。

### 1.2 目标（Iver 2026-05-09 决策档案）

- **目标 A · 主链路切 ChatDeepSeek**（M6 一次性做好，不延期到 M9）：DeepSeek 模型两端（官方 + 百炼）都走 `langchain_deepseek.ChatDeepSeek`，base_url 切端
- **目标 B · 方向 A 轻量抽象层**：`factory.py` 引入 provider registry（**只支持 deepseek + openai 两 provider**，不做 anthropic），`graph.py` 抽 `extract_finish_reason` / `extract_reasoning_content` helper，`state.py` 加 `provider` 字段透传——**finish_reason / reasoning 提取 helper 全量重构**（Iver 拍板 scope）
- **目标 C · Fallback 包装层留口子**：`with_fallbacks` 默认两 provider 双端，但加 `enable_fallback: bool` 开关（settings 字段）支持后期单端运行
- **目标 D · 探针 D 真机双端必过闸**：DeepSeek 原生 + 百炼 compat 各跑一次 ChatDeepSeek 真机，`reasoning_chunks > 0` 才能合并

### 1.3 不做什么（划界，防 scope 蔓延）

- ❌ **不做 anthropic provider 适配**（Iver 2026-05-09 决策；扩展空间留在 registry 注册点，代码不写）
- ❌ 不改 `me.py` / `sse.py` / `context.py` / `prompts.py` / `locks.py` 业务逻辑
- ❌ 不动 alembic / schema / `messages` / `sessions` 表
- ❌ 不动前端 / 不动 `dev_chat.py`
- ❌ 不引入 LangSmith / `with_structured_output` / `bind_tools`
- ❌ 不改 SSE 协议 / 不改 7 事件帧格式
- ❌ 不重命名 settings 字段（Iver 2026-05-09 反问后撤回「`settings.deepseek_model` 命名错配」定性，settings 按**供应商**分组的口径合理，本补丁不修）

## 二、当前架构现状（执行 agent 入场必读）

### 2.1 分支与 commit 链

- 当前分支：`feat/m6-main-chat-backend`（M6 主 plan 全部 commit 都在此分支线性继承）
- 接 commit 顺序紧接 Step 10 收口 commit 之后；本补丁所有 commit **继续在本分支**，PR 仍是同一个（最后整体 squash merge 到 main）
- 关键 commit 头（最新→最老，待执行 agent 入场后 `git log --oneline` 实拉对账）：
    - Step 10 收口：`<待回填，Iver 提供>` （test_chat_stream_stop_[keepgo.py](http://keepgo.py) 加 1 用例 `test_keepgo_inner_yield_connection_error`）
    - Step 9：`ef5c1d1`（chat stop endpoint）
    - Step 8c：`dd3afb7`（stop / 不 cancel / StopKind 二分支）
    - Step 8b：`b6e8599` + hotfix `645fd64`（接入 LangGraph 主图 + T5 + finish_reason 字段路径回退）
    - Step 8a：`70c36a5`（控制平面 + stub 流）
    - Step 2.5：`4716973`（plain-class `ChatDashScopeQwen` → `ChatOpenAI + with_fallbacks` 双 provider）

### 2.2 测试基线

- **375 passed deterministic** / ruff All checks passed / basedpyright 0 errors 0 warnings
- 验收必须保证 ≥375（factory / extractors 单测改写后总数会上升，回退视为 ⛔）

### 2.3 关键文件现状（影响范围）

**改动文件**：

- `backend/app/chat/factory.py` —— 当前是 `ChatOpenAI(base_url=...) + with_fallbacks` 双 provider；本补丁引入 `_PROVIDER_REGISTRY` + ChatDeepSeek 切换
- `backend/app/chat/graph.py` —— 当前 `call_main_llm` 内硬编码 `chunk.additional_kwargs["response_metadata"]["finish_reason"]` 解析；本补丁改为 helper 调用
- `backend/app/chat/state.py` —— 当前 `MainDialogueState` TypedDict；本补丁加 `provider: str` 字段
- `backend/app/core/settings.py` —— 当前已有 `deepseek_*` 系列字段（按供应商分组）+ 百炼相关字段；本补丁加 `main_provider` / `enable_fallback` / 探针 D 用的 base_url 字段（**实际字段名以源码为准**，执行 agent 入场后先 `cat backend/app/core/settings.py` 实拉对账）
- `backend/tests/chat/test_factory.py` —— 当前是 ChatOpenAI 双 provider 单测；本补丁改写为 registry 单测
- `backend/tests/chat/test_graph.py` —— 当前 4 测试 chunk 构造用 `additional_kwargs={"response_metadata": {...}}`；本补丁 helper 调用后保持等价

**新建文件**：

- `backend/app/chat/extractors.py` —— 新建，存 `extract_finish_reason` / `extract_reasoning_content` helper
- `backend/tests/chat/test_extractors.py` —— 新建，单测每 provider × 每字段位置
- `backend/scripts/verify/chatdeepseek_dual_provider_probe.py` —— 新建，探针 D

**不动文件**（违反即 ⛔）：

- `backend/app/api/me.py`（仅加 `provider=settings.main_provider` 到 initial_state 一行，本业务逻辑 0 变动）
- `backend/app/chat/sse.py` / `backend/app/chat/context.py` / `backend/app/chat/prompts.py` / `backend/app/chat/locks.py`
- 任何 `backend/app/api/*.py`（除 [me.py](http://me.py) 上述一行）
- `backend/alembic/` 整目录
- `frontend/` 整目录

### 2.4 关键事实档案（避免再走弯路）

- `chunk.additional_kwargs["response_metadata"]["finish_reason"]` 是 langchain-openai 兼容层的**真实路径**；`chunk.response_metadata` 顶层属性恒 `{}`（LangChain Pydantic 字段默认值行为，**不是**真实数据位置）。事实补丁档案见 [M6 · 执行偏差记录](https://www.notion.so/M6-ae216175294b41418ad609103ed3c494?pvs=21) §Step 2.5 事实补丁二次修正 + §Step 8b
- ChatDeepSeek 继承 `BaseChatOpenAI`，**finish_reason 路径相同**（`additional_kwargs.response_metadata.finish_reason`），**但额外**保留 `additional_kwargs.reasoning_content`（这是切换的核心收益）
- finish_reason 白名单：`stop` / `length` / `content_filter`，非白名单（如 `tool_calls`）不透传
- DeepSeek 思考模式参数：`/chat/completions` body 接受 `thinking={"type": "enabled"}` + 顶层 `reasoning_effort`；ChatDeepSeek 是否在顶层暴露 `reasoning_effort` 还是要走 `extra_body` 透传——**执行 agent 必须先翻 `langchain_deepseek/chat_models.py` 源码确认**（红线：第三方库行为先翻源码 / changelog / 文档，不准行为反推）

## 三、执行步骤（5 子步 · 一次验收）

### Step 11.1 · provider registry + ChatDeepSeek 切换

**改动范围**：`backend/app/chat/factory.py` + `backend/app/core/settings.py` + `pyproject.toml` + `backend/tests/chat/test_factory.py`

**任务**

- [ ]  `pyproject.toml` `[project].dependencies` 加 `langchain-deepseek>=0.1`，跑 `uv pip freeze > requirements.txt` 重生 lock（**禁止手编 requirements.txt**）
- [ ]  `factory.py` 引入 `_PROVIDER_REGISTRY: dict[str, Callable[[Settings], BaseChatModel]]`：
    - `"deepseek"` → 构建 `ChatDeepSeek(api_key=..., base_url=..., model=...)`，base_url 由调用方按主端 / fallback 端注入；thinking 参数透传见 Step 11.3
    - `"openai"` → 构建 `ChatOpenAI(api_key=..., base_url=..., model=...)`，预留 M11+ 多模型实验用（**本补丁不实际投产，只是 registry 注册位**）
- [ ]  `factory.py` 入口 `build_main_llm(settings) -> Runnable`：
    - 主 provider = `settings.main_provider`（新增字段，默认 `"deepseek"`），用主端 base_url 构建 primary
    - fallback provider = `settings.fallback_provider`（新增字段，默认 `"deepseek"` 即同一 provider 双端 base_url；`None` 时单端）
    - `settings.enable_fallback`（新增字段，默认 `True`）：`False` 时直接返回 primary，不包 `with_fallbacks`
    - `settings.enable_fallback=True` 时返回 `primary.with_fallbacks([secondary]).with_retry(...)`
- [ ]  `factory.py` 暴露 `ProviderNotRegistered(Exception)`，未注册的 provider 名抛此异常
- [ ]  `settings.py` 新增字段（如已有等价字段则复用，**先 `cat` 实拉再决定**）：
    - `main_provider: str = "deepseek"`
    - `fallback_provider: str | None = "deepseek"`
    - `enable_fallback: bool = True`
    - 双端 base_url：复用现有 `deepseek_base_url`（DeepSeek 原生）+ 现有百炼字段（按现状命名，**不重命名**）
- [ ]  改写 `tests/chat/test_factory.py`：
    - registry 注册查询：`_PROVIDER_REGISTRY["deepseek"]` / `["openai"]` 可调用
    - `build_main_llm` 默认 settings → 返回 `RunnableWithFallbacks` 实例，primary 是 `ChatDeepSeek`
    - `enable_fallback=False` → 返回 `ChatDeepSeek` 单实例（不是 `RunnableWithFallbacks`）
    - `main_provider="unknown"` → `ProviderNotRegistered`
    - mock 验证 ChatDeepSeek 构造入参（base_url / api_key / model）正确

**Commit message**

```
refactor(chat): introduce provider registry and switch main path to ChatDeepSeek

- factory: _PROVIDER_REGISTRY dispatching by settings.main_provider
- factory: build_main_llm with optional with_fallbacks (settings.enable_fallback)
- settings: main_provider / fallback_provider / enable_fallback fields
- requirements: add langchain-deepseek
- tests/chat/test_factory.py: rewrite for registry + ChatDeepSeek primary
```

### Step 11.2 · `extractors.py` 新建 + helper 全量重构

**改动范围**：`backend/app/chat/extractors.py`（新建）+ `backend/app/chat/graph.py` + `backend/app/chat/state.py` + `backend/tests/chat/test_extractors.py`（新建）+ `backend/tests/chat/test_graph.py` + `backend/app/api/me.py`（仅加一行 initial_state 透传）

**任务**

- [ ]  新建 `backend/app/chat/extractors.py`，导出两个 helper：

```python
def extract_finish_reason(chunk: AIMessageChunk, provider: str) -> str | None:
    """Extract finish_reason by provider. Returns None if not present or not in whitelist.

    deepseek / openai: chunk.additional_kwargs.response_metadata.finish_reason
    Whitelist: stop / length / content_filter (others discarded).
    """

def extract_reasoning_content(chunk: AIMessageChunk, provider: str) -> str | None:
    """Extract reasoning content (thinking text) by provider. None if absent.

    deepseek: chunk.additional_kwargs.reasoning_content (ChatDeepSeek extracts it)
    openai:   None (ChatOpenAI does NOT extract third-party reasoning;
              future OpenAI Responses API content blocks land here, M11+).
    """
```

- [ ]  `state.py` `MainDialogueState` TypedDict 加 `provider: str` 字段（从 factory 透传，[me.py](http://me.py) 在装 initial_state 时填入 `settings.main_provider`）
- [ ]  `graph.py` `call_main_llm` 节点：硬编码字段路径全部替换为 `extract_finish_reason(chunk, state["provider"])` / `extract_reasoning_content(chunk, state["provider"])`；payload dict 透出格式不变（`{"delta": text}` / `{"finish_reason": fr}` / `{"reasoning": rt}` 三类）
- [ ]  `me.py` 在装 `initial_state` 时填入 `provider=settings.main_provider`（**这是 [me.py](http://me.py) 唯一一行改动**，不算违反 §1.3 不改 [me.py](http://me.py) 业务逻辑边界——本字段仅是 initial_state 透传，无业务行为变化）
- [ ]  新建 `tests/chat/test_extractors.py`：
    - `extract_finish_reason`：deepseek / openai × {stop / length / content_filter / tool_calls / 路径缺失} 全覆盖
    - `extract_reasoning_content`：deepseek 走 `additional_kwargs.reasoning_content` 命中 / 缺失；openai 恒返回 `None`
    - 未注册 provider 字符串：默认走 deepseek 路径或返回 None（具体行为执行 agent 拍板 + 文档化即可）
- [ ]  改 `tests/chat/test_graph.py` 4 测试：chunk 构造保持现状（`additional_kwargs={"response_metadata": {"finish_reason": ...}}`），断言改为 helper 调用后的 payload dict 等价，**测试数量不减**

**Commit message**

```
refactor(chat): extract finish_reason and reasoning helpers per provider

- new chat/extractors.py: extract_finish_reason / extract_reasoning_content
- state: add provider field to MainDialogueState
- graph.call_main_llm: replace inline parsing with helper calls
- me.py: pass settings.main_provider into initial_state.provider
- tests/chat/test_extractors.py: per-provider × per-field coverage
- tests/chat/test_graph.py: refit assertions to helper payloads (count unchanged)
```

### Step 11.3 · ChatDeepSeek thinking 参数透传

**前置硬纪律**：执行 agent **必须先翻 `pip show langchain-deepseek` 拿到源路径 → `cat $SITE_PACKAGES/langchain_deepseek/chat_models.py` 实拉源码确认**：

- ChatDeepSeek 是否在顶层暴露 `reasoning_effort` / `thinking` 字段
- 还是必须走 `extra_body={"thinking": {...}, "reasoning_effort": "high"}` 透传到底层 OpenAI SDK
    - 源码若两者都支持，**优先选顶层字段**（API 稳定性更高）
    - 源码若仅支持 `extra_body`，则 factory 构建时传 `extra_body`

禁止任何「我猜应该是顶层字段」「ChatOpenAI 同款」之类的行为反推（红线：证据优先于推导，本项目累计 8+ 次踩坑）。

**任务**

- [ ]  翻 `langchain_deepseek/chat_models.py` 源码，记录 thinking 参数的真实路径（源码片段 + 行号写进 commit message + 偏差记录）
- [ ]  `factory.py` 构建 ChatDeepSeek 时按真实路径传 thinking 启用 + reasoning_effort（默认 `"high"`，settings 加字段 `deepseek_reasoning_effort: str = "high"`）
- [ ]  `tests/chat/test_factory.py` 加用例：mock `ChatDeepSeek.__init__` / `BaseChatOpenAI.__init__` 验证 thinking 参数到达底层 OpenAI SDK（断言 `extra_body` 或顶层字段取决于源码确认）

**Commit message**（thinking 参数路径以源码确认为准，下面是占位）

```
feat(chat): enable DeepSeek thinking mode via ChatDeepSeek

- factory: pass <extra_body|top-level> based on langchain_deepseek source (line N)
- settings: deepseek_reasoning_effort field (default "high")
- tests/chat/test_factory.py: mock SDK call assertion for thinking param path
```

### Step 11.4 · 探针 D · 真机双端验证（验收必过闸）

**改动范围**：`backend/scripts/verify/chatdeepseek_dual_provider_probe.py`（新建）

**前置约定**：base_url 双端**从 settings 读**（Iver 2026-05-09 拍板）——脚本不接受 CLI args，不硬编码 URL；api_key / model 也从 settings 读（settings 已加载 `.env`）。

**任务**

- [ ]  新建 `backend/scripts/verify/chatdeepseek_dual_provider_probe.py`：

```python
"""M6 收尾补丁 · 探针 D · ChatDeepSeek 双端 reasoning_content 真机验证。

用法: python -m backend.scripts.verify.chatdeepseek_dual_provider_probe
依赖: settings 已加载 .env，含 DeepSeek 原生 base_url + 百炼 base_url + 双端 api_key + model
输出: stdout JSON {"d1": {...}, "d2": {...}, "verdict": "pass|fail"}
闸门: d1.reasoning_chunks > 0 AND d2.reasoning_chunks > 0 → pass
"""
from backend.app.core.settings import settings
from langchain_deepseek import ChatDeepSeek
import asyncio, json

PROBE_PROMPT = "3 + 5 等于多少？请仔细思考后回答。"

async def probe(label: str, base_url: str, api_key: str, model: str) -> dict:
    llm = ChatDeepSeek(
        api_key=api_key, base_url=base_url, model=model,
        # thinking 参数路径以 Step 11.3 源码确认为准，此处占位
    )
    reasoning_chunks, content_chunks = 0, 0
    reasoning_text, content_text = "", ""
    async for chunk in llm.astream(PROBE_PROMPT):
        r = (chunk.additional_kwargs or {}).get("reasoning_content")
        if r:
            reasoning_chunks += 1
            reasoning_text += r
        if chunk.content:
            content_chunks += 1
            content_text += chunk.content
    return {
        "label": label, "base_url": base_url, "model": model,
        "reasoning_chunks": reasoning_chunks, "content_chunks": content_chunks,
        "reasoning_text_len": len(reasoning_text), "content_text_len": len(content_text),
        "reasoning_text_head": reasoning_text[:80], "content_text_head": content_text[:80],
    }

async def main():
    d1 = await probe("deepseek-native",
                     settings.deepseek_base_url,
                     settings.deepseek_api_key,
                     settings.deepseek_model)
    d2 = await probe("bailian-compat",
                     settings.<bailian_base_url>,
                     settings.<bailian_api_key>,
                     settings.<bailian_model>)
    verdict = "pass" if d1["reasoning_chunks"] > 0 and d2["reasoning_chunks"] > 0 else "fail"
    print(json.dumps({"d1": d1, "d2": d2, "verdict": verdict}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ]  字段名占位 `<bailian_*>` 实拉 `settings.py` 后回填；如百炼端字段名不存在则 Step 11.1 加上（settings 实际字段名以源码为准）
- [ ]  **执行 agent 跑探针 + 回贴 stdout JSON 输出**到本页 §七「探针 D 实测结果」段（Step 11.5 收尾时回写）
- [ ]  **闸门 B 否决条件**：D-1 或 D-2 任一 `reasoning_chunks=0` → 整包 ⛔ 不通过
- [ ]  探针**不入 pytest** suite（依赖外部 API key，会引入 flaky 与成本）

**Commit message**

```
test(verify): add ChatDeepSeek dual-provider reasoning_content probe (D)

- backend/scripts/verify/chatdeepseek_dual_provider_probe.py
- reads base_url / api_key / model from settings (no CLI args)
- emits JSON verdict; reasoning_chunks > 0 on both endpoints required
```

### Step 11.5 · 验收 + 偏差登记 + PR 描述更新

**任务**

- [ ]  `pytest backend/tests` 全绿，`passed deterministic` 数 ≥ **375**（factory / extractors / graph 单测改写后会上涨，回退则 ⛔）
- [ ]  `ruff check backend/` All checks passed / `basedpyright backend/` 0 errors 0 warnings
- [ ]  探针 D 跑两端 + 输出 JSON，`verdict=pass` 才能进闸门 B
- [ ]  偏差全量回写到 [M6 · 执行偏差记录](https://www.notion.so/M6-ae216175294b41418ad609103ed3c494?pvs=21) §Step 11.x 段（5 子步各自一段 + 整包决策档案 + Iver 2026-05-09 决策档案 + DeepSeek/百炼 Responses 文档 4 证据搬入设计档案 §十）
- [ ]  PR description 在原 13 commits 之后追加 5 行 Step 11.x commits + 探针 D JSON 输出截图
- [ ]  把本页§七「探针 D 实测结果」段填入真实 stdout JSON
- [ ]  等闸门 B（NoNo 远端核验：GitHub commits API 实拉每个 commit metadata + verbatim 核对关键 patch + 探针 D 输出 JSON 对账）通过后，squash merge `feat/m6-main-chat-backend → main`，回 main 跑 `pytest` + `alembic upgrade head` 验证不退化

**Commit message**

```
docs(m6): record llm-abstraction patch verification and deviations
```

## 四、验收清单（一次性 · 整包）

- [ ]  `factory.py` 双 provider registry 就位（deepseek / openai 两键，未注册抛 `ProviderNotRegistered`）
- [ ]  `build_main_llm` 默认返回 `RunnableWithFallbacks`（双端 ChatDeepSeek），`enable_fallback=False` 返回单 ChatDeepSeek 实例
- [ ]  `langchain-deepseek` 在 `requirements.txt` lock 内
- [ ]  `chat/extractors.py` 新建 + 单测覆盖每 provider × 每字段位置
- [ ]  `state.py` `provider` 字段就位 + `me.py` initial_state 填入
- [ ]  `graph.py` `call_main_llm` 内无硬编码字段路径，全部走 helper
- [ ]  DeepSeek thinking 参数透传路径**有源码引用**（`langchain_deepseek/chat_models.py` 行号写进 commit message + 偏差记录）
- [ ]  探针 D 双端 `reasoning_chunks > 0`（必过闸）
- [ ]  `pytest backend/tests` ≥ 375 deterministic / ruff 全绿 / basedpyright 0 errors
- [ ]  M6 plan 主页 §6 “相关文档” 或 §五 发现与建议 中加一行指向本页
- [ ]  偏差登记完成（[M6 · 执行偏差记录](https://www.notion.so/M6-ae216175294b41418ad609103ed3c494?pvs=21) §Step 11.x）
- [ ]  PR description 更新（原 13 commits + 本补丁 5 commits + 探针 D 输出）

## 五、回归保护

- 现有 `me.py` 业务逻辑不改一行（仅 initial_state 加 `provider` 字段透传，无行为变化）
- 现有 `sse.py` / `context.py` / `prompts.py` / `locks.py` 0 改动
- alembic / schema / 前端 / dev_chat 0 改动
- M6 主 plan 13 commit 全部保留 + 本补丁 5 commit 线性继承
- 测试基线 375 passed deterministic 不退化（只能上升）

## 六、红线提醒（执行 agent 必读 · 5 条）

1. **证据优先于推导**——本项目累计 8+ 次踩坑；结论必须有源码 / changelog / 文档引用，不准行为反推「v1.2.1 是 bug，v1.3.0 修了」之类的话
2. **第三方库行为先翻源码**——尤其 `langchain_deepseek` 的 thinking 参数路径，不读源码不准写代码（Step 11.3 硬纪律）
3. **真机探针 D 不可跳**——双端 ChatDeepSeek `reasoning_chunks > 0` 是闸门 B 否决项；mock 单测过了不代表真机过
4. **settings 命名口径**——按**供应商**分组（`deepseek_*` 一组、百炼一组），不是按端点 host 分组；本补丁不改任何已有字段名（Iver 2026-05-09 反问后撤回「错配」定性）
5. **业务逻辑边界**——本补丁只动 `factory.py` / `extractors.py`（新建）/ `state.py` / `graph.py call_main_llm 节点内` / `settings.py` 字段新增 / `me.py` initial_state 一行；越界即 ⛔

## 七、探针 D 实测结果

*（待 Step 11.5 收尾时执行 agent 回写真实 stdout JSON）*

```json
{
  "d1": {"label": "deepseek-native", "reasoning_chunks": 0, "content_chunks": 0, "...": "待回填"},
  "d2": {"label": "bailian-compat", "reasoning_chunks": 0, "content_chunks": 0, "...": "待回填"},
  "verdict": "<待回填>"
}
```

## 八、相关文档

- M6 主 plan：[M6 · 主对话链路 - 后端核心 — 实施计划 (6/17)](https://www.notion.so/M6-6-17-a36bdd99fc0f445d86623025c330ea0c?pvs=21)
- 设计基线：[M6–M9 · 主对话链路 — 设计基线](https://www.notion.so/M6-M9-36d3c417e0d1406385868f912bcb7c45?pvs=21)
- 架构基线：[技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21)（§十 LLM 客户端）
- 偏差记录：[M6 · 执行偏差记录](https://www.notion.so/M6-ae216175294b41418ad609103ed3c494?pvs=21)（5 子步偏差登记目的地）
- 妥协跟踪：[](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)
- 步骤执行 skill：[Step-Execute Skill v1.6 更新稿](https://www.notion.so/Step-Execute-Skill-v1-6-a92066d4fc6f43a8b3cc177c55c1d560?pvs=21)
- 步骤偏差审核：[Agent 指引 · 步骤差异审核](https://www.notion.so/Agent-d2d23aab6c7e44b899783ba60af9e6f0?pvs=21)
- 路线图：[执行规划：17 个里程碑](https://www.notion.so/17-de81294334b947ef8d598245c73832ad?pvs=21)