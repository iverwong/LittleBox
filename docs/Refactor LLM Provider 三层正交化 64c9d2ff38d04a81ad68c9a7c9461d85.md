# Refactor: LLM Provider 三层正交化

<aside>
🔍

**分支**：`refactor/llm-provider-orthogonal`（首 Step 建）

**基线（fork 点）** `refactor/backend-audit-phase-1` 当前 HEAD

**性质**：`backend/app/core/llm.py` provider 架构正交化重构。无功能新增；唯一运行时行为变化 = main / compression 获得真实百炼兜底。

**待办来源**：本页「待办记录」第 2 条（[llm.py](http://llm.py) 架构）。

</aside>

## 目标概述

把 `core/llm.py` 现在「按 `role×品牌×client` 复合字符串 key」的缠绕式 provider 工厂，重切为**三层正交的 code 声明**（单一真相源，全部进 code，仅 API 密钥留 `.env`）：

1. **端点表**：`{base_url(进 code), 读哪个密钥}`。
2. **模型档**（按模型族）：`{transport 类, 是否 reasoning 提取, 工具/多模态能力}`——**transport 归这层，由 model 决定，与端点/role 无关**。
3. **role 绑定**：`{endpoint, model, thinking, reasoning_effort, temperature, fallback}`。crisis / redline = main 别名（接替对话，需流式+思考、不绑工具，行为同 main 而非 audit）。

构建链：`role → (endpoint, model) → 模型档给 transport+方言 → 实例化`。

**同时消除的既有缺陷**：

- 陷阱①（同一 `(bailian, deepseek-v4)` 因 role 不同被 ChatDeepSeek / ChatOpenAI 两种 client 触达）→ transport 由模型档唯一决定，结构上不可能再分叉。
- main 假兜底（`fallback_provider="deepseek"` → deepseek→deepseek）→ 改为 deepseek→bailian 真冗余，且兜底端带思考。
- compression 无兜底 → 补 bailian 兜底（满足「每个 role 都要兜底」）。
- `main_provider` / `audit_provider` 在 crisis/redline 的 LLM 与 extractor 间口径不一致的潜在 bug → 统一从 role 绑定单一推导。
- reasoning 方言泄漏到图层（`graph.py` 传 provider 字符串给 extractor）→ 改由模型档承载。

### 不做什么（明确排除）

- **不实现** qwen-vl / MiMo 等新模型族的 adapter——仅在模型档注册表留注释位 + 解析报错路径；真要用时「写 adapter + 真机探针」再启用。
- **不换任何默认模型**：全线仍 `deepseek-v4-flash`。
- **不重构 streaming fallback 语义**：沿用现有 `with_fallbacks` 行为（中途已出首 token 后的失败不保证干净切换），只改兜底目标。
- **不调** retry 数值（仍 3 次 + 指数抖动 + 3 类异常）、不动 prompt、不动多模态 content 格式。
- **不动** 非 LLM-provider 的 settings（DB / Redis / 审查编排参数 `audit_wait_timeout_seconds` 等一律保留）。
- **不动** M8-hotfix 的 `_convert_message_to_dict` monkeypatch 及其版本断言（它服务于未来 ChatOpenAI 路径，保持原样）。

## 前置条件

- 基线 `39178da1` 上 `pytest tests -q` 全绿（本页记录 Phase 0 已修绿至 `a0c025a`，需确认 HEAD 链路无新漂移）。
- 容器内运行：`docker compose exec api ...`（宿主无 Python / uv）。
- 运行时 Python 3.14、DeepSeek V4 系列（`deepseek-v4-flash` / `-pro` 为合法枚举，勿用历史 `deepseek-chat`）。
- 执行 agent 拥有完整仓库上下文；本计划只贴**新增契约**，既有实现（如 `_build_chat_deepseek` 的 `extra_body` 构造、`graph.py` 流式消费体）以自然语言引用。

## 现状 → 目标映射（自包含速查）

| 现 public key          | _parse_key              | 现 transport                 | 目标落点                                                  |
| ---------------------- | ----------------------- | ---------------------------- | --------------------------------------------------------- |
| `deepseek`             | (main, deepseek)        | ChatDeepSeek@deepseek        | ROLES.main 主端                                           |
| `openai` ⚠️             | (main, openai)          | ChatOpenAI@bailian（丢思考） | 删除；ROLES.main.fallback 改 ChatDeepSeek@bailian         |
| `audit_deepseek`       | (audit, deepseek)       | ChatDeepSeek@deepseek        | ROLES.audit 主端                                          |
| `audit_bailian`        | (audit, bailian)        | ChatDeepSeek@bailian         | ROLES.audit.fallback（行为不变）                          |
| `compression_deepseek` | (compression, deepseek) | 独立 builder                 | ROLES.compression 主端（temperature/effort 降为可选字段） |

## 执行步骤

### Step 0 · 建分支 + 基线核实

- [ ]  `git checkout refactor/backend-audit-phase-1 && git pull`
- [ ]  `git checkout -b refactor/llm-provider-orthogonal`
- [ ]  容器内 `pytest tests -q` 全绿；记录基线通过数
- [ ]  拉 `backend/tests/test_factory.py`，清点针对 `_PROVIDER_REGISTRY` / `_PUBLIC_KEYS` 的断言（约 16+ 处，T0/T1/T5/T5c），列为 Step 7 重写清单

**验证**：✅ 分支建立；✅ 基线全绿；✅ 待重写断言清单成形。

**Commit**：无（仅建分支 + 核实）。

### Step 1 · 新增三表（拓扑单一真相源）

新建 `backend/app/core/llm_topology.py`，以 frozen dataclass 声明三层。**base_url 与全局超时进 code**；端点仅记录读哪个密钥字段。

```python
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pydantic import SecretStr
from app.core.config import Settings

LLM_REQUEST_TIMEOUT_SECONDS = 60.0  # 全局超时进 code（无 dev/prod 区分）

# —— 枚举：把散落字符串固化进类型系统（StrEnum 保留 str 兼容 + 穷尽性 + 防 typo）——
class Role(StrEnum):
    MAIN = "main"
    AUDIT = "audit"          # crisis / redline 复用
    COMPRESSION = "compression"

class EndpointName(StrEnum):
    DEEPSEEK = "deepseek"
    BAILIAN = "bailian"

class Transport(StrEnum):
    CHAT_DEEPSEEK = "chat_deepseek"
    CHAT_OPENAI = "chat_openai"   # 未实现，留给未来 qwen-vl
    CHAT_TONGYI = "chat_tongyi"   # 未实现

class ReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"

# —— 第 1 层：端点（base_url 进 code；api_key 用 getter，去掉 getattr+str 洞）——
@dataclass(frozen=True)
class Endpoint:
    name: EndpointName
    base_url: str
    api_key: Callable[[Settings], SecretStr]

ENDPOINTS: dict[EndpointName, Endpoint] = {
    EndpointName.DEEPSEEK: Endpoint(EndpointName.DEEPSEEK,
        "https://api.deepseek.com/v1", lambda s: s.deepseek_api_key),
    EndpointName.BAILIAN: Endpoint(EndpointName.BAILIAN,
        "https://dashscope.aliyuncs.com/compatible-mode/v1", lambda s: s.bailian_api_key),
}

# —— 第 3 层：模型档（family = 模型名匹配前缀，是这层的身份/主键）——
@dataclass(frozen=True)
class ModelProfile:
    family: str               # 匹配前缀："deepseek-v4" 命中 -flash / -pro；注册表唯一主键
    transport: Transport
    supports_reasoning: bool  # 是否走 reasoning_content 提取
    supports_tools: bool
    multimodal: bool

# 用元组而非 dict：family 作单一来源，不再与 dict key 重复
MODEL_PROFILES: tuple[ModelProfile, ...] = (
    ModelProfile("deepseek-v4", Transport.CHAT_DEEPSEEK, True, True, False),
    # ModelProfile("qwen-vl", Transport.CHAT_OPENAI, True, True, True),  # 写 adapter+探针后启用
)

class ModelProfileNotRegisteredError(LookupError):
    """模型名无对应模型档时抛出。"""

def resolve_profile(model: str) -> ModelProfile:
    matches = [p for p in MODEL_PROFILES if model.startswith(p.family)]
    if not matches:
        raise ModelProfileNotRegisteredError(
            f"模型 {model!r} 无对应模型档；新族需先写 adapter 并通过真机探针")
    return max(matches, key=lambda p: len(p.family))  # 最长前缀优先，防族前缀重叠误命中

# —— 第 2 层：role 绑定 ——
@dataclass(frozen=True)
class RoleBinding:
    endpoint: EndpointName
    model: str                # 具体模型名（开放数据），由 resolve_profile 在构建时校验有档
    thinking: bool
    reasoning_effort: ReasoningEffort | None
    temperature: float | None
    fallback: RoleBinding | None = None

_DSV4 = "deepseek-v4-flash"
ROLES: dict[Role, RoleBinding] = {
    Role.MAIN: RoleBinding(EndpointName.DEEPSEEK, _DSV4, True, ReasoningEffort.MAX, None,
        fallback=RoleBinding(EndpointName.BAILIAN, _DSV4, True, ReasoningEffort.MAX, None)),
    Role.AUDIT: RoleBinding(EndpointName.DEEPSEEK, _DSV4, True, ReasoningEffort.MAX, None,
        fallback=RoleBinding(EndpointName.BAILIAN, _DSV4, True, ReasoningEffort.MAX, None)),
    Role.COMPRESSION: RoleBinding(EndpointName.DEEPSEEK, _DSV4, False, None, 0.3,
        fallback=RoleBinding(EndpointName.BAILIAN, _DSV4, False, None, 0.3)),
}
# crisis / redline 复用 ROLES[Role.MAIN]（接替对话：流式+思考、不绑工具，行为同 main）
```

**验证**：✅ ruff / basedpyright 0 错；✅ `resolve_profile("deepseek-v4-flash")` 命中、未知名抛 `ModelProfileNotRegisteredError`（可先写本步的纯函数单测）。

**Commit**：`refactor(llm): 新增 llm_topology 三表（端点/模型档/role 绑定）`

### Step 2 · adapter 层（transport 由模型档分发）

在 `llm.py` 实现「绑定 → 实例」的装配。deepseek-v4 adapter 复用现 `_build_chat_deepseek` 的构造逻辑（`api_base` / `model` / `timeout` / `max_retries=0` / `extra_body{thinking.type, reasoning_effort}`），但把 `reasoning_effort` 与 `temperature` 降为**可选**——`compression` 的 `temperature=0.3 & effort=None` 自然落位，陷阱②的独立 builder 随之消失。

```python
from typing import Any
from langchain_core.language_models import BaseChatModel

def _adapter_chat_deepseek(api_key: str, base_url: str, b: RoleBinding) -> BaseChatModel:
    # extra_body / kwargs 的 Any 是第三方 SDK 构造边界，正当；与领域级 Any 不同
    extra_body: dict[str, Any] = {"thinking": {"type": "enabled" if b.thinking else "disabled"}}
    if b.reasoning_effort is not None:
        extra_body["reasoning_effort"] = b.reasoning_effort.value  # StrEnum → 原始字符串
    kwargs: dict[str, Any] = dict(api_key=api_key, api_base=base_url, model=b.model,
                        timeout=LLM_REQUEST_TIMEOUT_SECONDS, max_retries=0,
                        extra_body=extra_body)
    if b.temperature is not None:
        kwargs["temperature"] = b.temperature
    return ChatDeepSeek(**kwargs)

# 键为 Transport 枚举；只映射已实现的传输（未实现的枚举值不出现在任何已注册模型档里）
_TRANSPORTS: dict[Transport, Callable[[str, str, RoleBinding], BaseChatModel]] = {
    Transport.CHAT_DEEPSEEK: _adapter_chat_deepseek,
}

def _build_binding(b: RoleBinding, settings: Settings) -> BaseChatModel:
    ep = ENDPOINTS[b.endpoint]
    profile = resolve_profile(b.model)
    api_key = ep.api_key(settings).get_secret_value()  # 无 getattr、无领域 Any
    return _TRANSPORTS[profile.transport](api_key, ep.base_url, b)
```

**验证**：✅ ruff / basedpyright 0 错；✅ 单测：main 主端 → `ChatDeepSeek`，`extra_body.thinking.type=enabled` 且含 `reasoning_effort=max`；compression 主端 → 含 `temperature=0.3`、不含 `reasoning_effort`。

**Commit**：`refactor(llm): deepseek-v4 adapter + transport 分发`

### Step 3 · role-based build + 删除字符串注册表

- [ ]  新增 `build_role_primary(role, settings)` / `build_role_fallback(role, settings)` / `wrap_resilience(primary, fallback)`（retry+fallback 包装，复用现 `build_main_llm` 的 retry 参数）
- [ ]  重写 `build_main_llm` = `wrap_resilience(primary, fallback)`（role="main"）
- [ ]  重写 `build_crisis_llm` / `build_redline_llm` = role="main" 的 `wrap_resilience`（**不** bind_tools，与现行为一致），删除 `f"audit_{main_provider}"` 字符串拼接
- [ ]  **删除** `_PROVIDER_REGISTRY` / `_PUBLIC_KEYS` / `_parse_key` / `_CLIENT_BUILDER` / `_MODEL_FIELD` / `_ROLE_SETTINGS` / `_build_chat_openai`(暂留给未来或移走) / `_build_compression_deepseek`
- [ ]  测试注入缝 `_test_llm_overrides` 改为 **按 role 键**：`set_test_llm(role, llm)` / `clear_test_llm`，在各 `build_*_llm` 入口优先返回 override

```python
_test_llm_overrides: dict[Role, BaseChatModel] = {}  # key = Role 枚举

def build_role_primary(role: Role, settings: Settings) -> BaseChatModel:
    if role in _test_llm_overrides:
        return _test_llm_overrides[role]
    return _build_binding(ROLES[role], settings)

def build_role_fallback(role: Role, settings: Settings) -> BaseChatModel | None:
    fb = ROLES[role].fallback
    return _build_binding(fb, settings) if fb else None

def wrap_resilience(primary: Runnable, fallback: Runnable | None) -> Runnable:
    from openai import APIConnectionError, APITimeoutError, RateLimitError
    retryable = primary.with_retry(
        retry_if_exception_type=(RateLimitError, APITimeoutError, APIConnectionError),
        stop_after_attempt=3, wait_exponential_jitter=True)
    return retryable.with_fallbacks([fallback]) if fallback else retryable

def build_main_llm(settings: Settings) -> Runnable:
    return wrap_resilience(build_role_primary(Role.MAIN, settings),
                          build_role_fallback(Role.MAIN, settings))
```

<aside>
⚠️

**保留** M8-hotfix 的 `_convert_message_to_dict` monkeypatch 段与 `_VERIFIED_LCO_VERSIONS` 断言**原样不动**——它服务于未来 ChatOpenAI 路径，当前虽无 role 走 ChatOpenAI 也无害。

</aside>

**验证**：✅ ruff / basedpyright 0 错（pytest 全绿留到 Step 7，本步标 ⏸）。

**Commit**：`refactor(llm): role 驱动构建，移除字符串 provider 注册表`

### Step 4 · config.py 瘦身（LLM 拓扑出 settings）

从 `Settings` **删除**以下 LLM-provider 字段（已迁入 code）：`deepseek_base_url` / `deepseek_model` / `bailian_base_url` / `bailian_model` / `main_provider` / `fallback_provider` / `enable_fallback` / `main_thinking_enabled` / `main_reasoning_effort` / `audit_provider` / `audit_model` / `audit_reasoning_effort` / `audit_thinking_enabled` / `compression_provider` / `compression_model` / `compression_thinking_enabled` / `llm_request_timeout_seconds`。

**保留**（密钥，env 注入）：`deepseek_api_key` / `bailian_api_key`。其余非 LLM 字段全部保留。

- [ ]  删字段后全仓 grep 这些字段引用，逐处改为读 `llm_topology`（多数集中在 `llm.py`，Step 1-3 已替换）
- [ ]  `audit/llm.py`、`graph.py` 的残留引用在 Step 5/6 收口

**验证**：✅ ruff / basedpyright 0 错；✅ grep 确认删除字段零残留引用。

**Commit**：`refactor(config): LLM 拓扑移入 code，settings 仅留密钥`

### Step 5 · 提取器按模型档解耦

`llm_extractors.py`：`extract_reasoning_content` 改按**模型档**判定，不再按 provider 字符串；`extract_finish_reason` 去掉 provider 入参（白名单逻辑不变）。新增 `role_profile(role)` helper。

```python
def role_profile(role: Role) -> ModelProfile:
    return resolve_profile(ROLES[role].model)

def extract_reasoning_content(chunk: BaseMessageChunk, profile: ModelProfile) -> str | None:
    if not profile.supports_reasoning:
        return None
    return (chunk.additional_kwargs or {}).get("reasoning_content")
```

`graph.py` 三处调用点：`call_main_llm` 传 `role_profile(Role.MAIN)`、`call_crisis_llm` / `call_redline_llm` 传 `role_profile(Role.MAIN)`（crisis/redline 复用 main），替换原 `ctx.settings.main_provider`（main）/ `ctx.settings.audit_provider`（crisis/redline）。`_stream_llm_chunks` 的 `provider` 形参改为 `profile`。

**验证**：✅ ruff / basedpyright 0 错；✅ 单测：deepseek-v4 档下 reasoning chunk 提取到 `reasoning_content`；✅ 流式 4 段派发行为不变（reasoning / delta / finish_reason / usage）。

**Commit**：`refactor(llm): reasoning/finish_reason 提取按模型档解耦`

### Step 6 · audit/llm.py 改用 role 绑定

`build_audit_llm` 改为：取 `build_role_primary("audit")` 与 `build_role_fallback("audit")`，**各自 `bind_tools([AppendNote, ReplaceInNotes, AuditOutputSchema])` 后再** `wrap_resilience`（保留「先 bind_tools 再包 retry/fallback」次序），删除对 `build_provider_llm("audit_deepseek"/"audit_bailian")` 的字符串调用。

**验证**：✅ ruff / basedpyright 0 错；✅ 单测：audit LLM 主备两端均绑定 3 个工具且 thinking=enabled。

**Commit**：`refactor(audit): audit LLM 改用 role 绑定`

### Step 7 · 重写 factory 测试 + 注入缝迁移

- [ ]  删除 `test_factory.py` 中针对 `_PROVIDER_REGISTRY` / `_PUBLIC_KEYS` / `_parse_key` 的旧断言（Step 0 清单），改为断言新结构：
    - `resolve_profile` 命中 / 未注册抛错
    - `build_role_primary("main")` → ChatDeepSeek@deepseek、`extra_body` 正确；fallback → @bailian
    - compression 主端 `temperature=0.3` 且无 `reasoning_effort`；有 bailian fallback
    - crisis/redline 解析到 **main 绑定**（同 main）且不绑工具
- [ ]  `conftest.py` 及集成测试中 `set_test_llm` 改用 `Role` 枚举键：旧 `"deepseek"`→`Role.MAIN`；旧 `"audit_deepseek"` **按用途拆分**——真审查 note-writer→`Role.AUDIT`，crisis/redline 干预→`Role.MAIN`
- [ ]  测试 docstring 用 Given/When/Then
- [ ]  **隔离铁律**：factory / extractor 测试为纯单测，无需 DB/Redis fixture；用到注入缝的集成测试一律经现有 `conftest.py` 的 `dependency_overrides` 进入，禁止真实连接 / subprocess / flushdb

**验证**：✅ `pytest tests -q` 全绿（恢复基线通过数）；✅ ruff / basedpyright 0 错。

**Commit**：`test(llm): 重写 factory 测试 + role 键注入缝`

### Step 8 · 真机探针 + 验收

手动探针（容器内、真实 API；非 pytest，不入 CI）：确认 **bailian + deepseek-v4 + ChatDeepSeek + thinking** 这条 main/compression 新兜底路径可用。注：该路径 audit 兜底今日已在用（tools+thinking），main 兜底为其子集（stream+thinking 无 tools），风险低，本步为确认而非探索。

- [ ]  `docker compose exec api python -c "..."`：以 `build_role_fallback("main", settings)` 实例发一轮带思考的流式请求，确认有 `reasoning_content` 且 200
- [ ]  多轮校验：思考轮后续请求回传 `reasoning_content`，不触发 400「reasoning_content missing」
- [ ]  端到端：本地起服务走一轮主对话，人为令主端失败（如临时改密钥）验证 fallback 切到百炼且思考链路正常

**验证**：✅ 兜底真机 200 + 思考；✅ 多轮无 400；✅ e2e fallback 生效；⏸ 任一不过则记录并回滚至对应 Step。

**Commit**：无（探针 + 验收）；里程碑完成后开 PR 并入 `refactor/backend-audit-phase-1` 或 main。

## 验收清单

| 项             | 判据                                                                   | 状态 |
| -------------- | ---------------------------------------------------------------------- | ---- |
| 三表落地       | 端点/模型档/role 绑定全在 code，settings 仅余密钥                      | ⏸    |
| 陷阱① 消除     | transport 仅由模型档决定，无 (role,provider) 二元分发                  | ⏸    |
| main 真兜底    | deepseek→bailian（ChatDeepSeek，带思考），非 deepseek→deepseek         | ⏸    |
| 每 role 有兜底 | main / audit / compression 均有 fallback                               | ⏸    |
| 方言解耦       | extractor 按模型档；[graph.py](http://graph.py) 不再传 provider 字符串 | ⏸    |
| 测试全绿       | pytest 恢复基线通过数；ruff / basedpyright 0 错                        | ⏸    |
| 真机探针       | bailian 兜底 200 + 思考 + 多轮无 400                                   | ⏸    |
| 行为边界       | 默认模型不变、retry 数值不变、streaming fallback 语义不变              | ⏸    |

## 发现与建议

1. **main 假兜底是真 bug，不止是架构不洁**：`fallback_provider="deepseek"` 令主对话兜底退化为同端点重试，百炼渠道冗余从未生效。本重构顺带修复，属行为改善而非纯重构——验收时重点回归主对话兜底路径。
2. **compression 新增 retry**：统一 `wrap_resilience` 后 compression 主端获得 3 次重试（原先无）。判断为有益且低风险；若要严格零行为变化，可在 ROLES 上给 compression 标记跳过 retry——默认不跳。
3. **streaming fallback 语义未改**：`with_fallbacks` 对「首 token 已发后的中途失败」不保证干净切换，这是 LangChain 既有限制，本次不碰。若未来要求主对话流式强一致兜底，需单独立项（缓冲首帧 / 重放）。
4. **ChatOpenAI 与 monkeypatch 暂时休眠但保留**：当前无 role 走 ChatOpenAI，`_convert_message_to_dict` 补丁不生效但保留就位；待 qwen-vl 等新族落 adapter 时复用。
5. **新族纪律固化**：`resolve_profile` 对未注册模型名直接抛错——「换已注册族的模型=改 ROLES + 过验证」「加新族=写 adapter + 真机探针（思考方言 / reasoning 多轮 / 工具 / 流式 / 多模态各验）后再注册」。
6. **crisis/redline 重锦到 main（非 audit）**：它们是接替主对话的流式干预（需思考、不绑工具），行为本就同 main 而非 note-writer 的 audit；原先落在 `audit_{main_provider}` 是历史巧合。今日 main/audit 绑定恰好等价，故此改**运行时零行为变化**，但纠正了语义锦点——日后 main 换模型/参数时 crisis/redline 自动跟随，不会误随 audit。