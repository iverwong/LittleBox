"""LLM Provider 三层正交化拓扑声明（单一真相源）。

把 `app/core/llm.py` 旧的「按 role×品牌×client 复合字符串 key」缠绕式
provider 工厂，重切为三层正交 code 声明：

1. **端点表** `ENDPOINTS`：base_url 进 code，api_key 走 getter 闭包；
   端点仅声明「读哪个密钥字段」，不直接持有 SecretStr。
2. **模型档** `MODEL_PROFILES`：按模型族声明 transport / reasoning / tools /
   multimodal 能力。transport 由模型档唯一决定，结构上不可能再分叉。
3. **role 绑定** `ROLES`：把 endpoint + model + 思考参数 + fallback + 重试次数
   绑定到 role（main / audit / compression）。crisis / redline 复用
   `ROLES[Role.MAIN]`，行为同 main（流式 + 思考、不绑工具），而非 audit。

构建链：`role → (endpoint, model) → 模型档给 transport+方言 → 实例化`。

新族纪律：
- `resolve_profile` 对未注册模型名直接抛 `ModelProfileNotRegisteredError`。
  - 换已注册族的模型 = 改 `ROLES` + 过验证
  - 加新族 = 写 adapter + 真机探针（思考方言 / reasoning 多轮 / 工具 /
    流式 / 多模态各验）后再注册

⚠️ **运行时类型解析警告**：
本模块 dataclass 的 `Callable[[Settings], SecretStr]` 注解依赖
`from __future__ import annotations` + `TYPE_CHECKING` 守卫
（Settings 仅在类型检查期可见）。子代理已实测：
- `__init__` / `repr` / `dataclasses.fields` / `asdict` / `replace` /
  `hash` / `__eq__` 全部正常（不解析注解）
- `typing.get_type_hints(Endpoint)` 会抛 `NameError: name 'Settings'
  is not defined`（自引用 `RoleBinding` 除外，因模块 globals 里有定义）
- pydantic 2.x `TypeAdapter(Endpoint)` 不触碰 dataclass（无 validator），
  LangGraph 1.2 / langchain 1.3 不在 import 期扫描用户 dataclass 注解

→ **调用方**不得对本模块任何 dataclass 调 `typing.get_type_hints`，
也禁止把 `Endpoint` / `RoleBinding` 直接喂给 pydantic `BaseModel` /
LangGraph typed dict / tool 装饰器。若未来需要序列化，先转成不含
`Settings` 前向引用的中间 dataclass。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import SecretStr

if TYPE_CHECKING:
    from app.core.config import Settings


# 全局超时进 code（无 dev/prod 区分）；Step 2 adapter 层读取此值构造 LangChain LLM。
LLM_REQUEST_TIMEOUT_SECONDS = 60.0


# —— 枚举：把散落字符串固化进类型系统（StrEnum 保留 str 兼容 + 穷尽性 + 防 typo）——
class Role(StrEnum):
    """主对话 / 审查 / 压缩三个 role。crisis / redline 复用 main（不另列枚举值）。"""

    MAIN = "main"
    AUDIT = "audit"
    COMPRESSION = "compression"


class EndpointName(StrEnum):
    """provider 端点枚举。今日只注册 deepseek / bailian 两个 base_url。"""

    DEEPSEEK = "deepseek"
    BAILIAN = "bailian"


class Transport(StrEnum):
    """LangChain chat model transport 枚举。

    CHAT_DEEPSEEK 今日唯一已实现。
    CHAT_OPENAI / CHAT_TONGYI 占位未实现——任何 ModelProfile 引用未实现 transport
    会在 Step 2 的 `_TRANSPORTS[T]` 查找中抛 KeyError。新族纪律：写 adapter + 真机
    探针后再启用。
    """

    CHAT_DEEPSEEK = "chat_deepseek"
    CHAT_OPENAI = "chat_openai"  # 未实现，留给未来 qwen-vl 等 OpenAI 兼容族
    CHAT_TONGYI = "chat_tongyi"  # 未实现


class ReasoningEffort(StrEnum):
    """推理深度档。Step 2 透传至 `extra_body["reasoning_effort"]` 作为字符串字面量。

    ⚠️ Python 3.11+ StrEnum 的 `str(e)` 返回 `value`（如 `'max'`）而非
    `'ReasoningEffort.MAX'`。Step 2 取字符串走 `.value` 显式路径，不依赖 `str()`。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


# —— 第 1 层：端点（base_url 进 code；api_key 用 getter，去掉 getattr+str 漏洞）——
@dataclass(frozen=True)
class Endpoint:
    """provider 端点声明：name + base_url + 密钥 getter。

    `api_key` 是 `Callable[[Settings], SecretStr]`，调用方传入 settings 拿到
    SecretStr 后自行 `.get_secret_value()`。不直接持有 SecretStr 是为了让密钥
    仍是 Settings 单一来源，避免本模块被 import 时就触发密钥求值。
    """

    name: EndpointName
    base_url: str
    api_key: Callable[[Settings], SecretStr]


# 两条 base_url 必须是裸字符串字面量，禁止尾随字符 / 不可见字符 / 误粘的链接语法。
ENDPOINTS: dict[EndpointName, Endpoint] = {
    EndpointName.DEEPSEEK: Endpoint(
        EndpointName.DEEPSEEK,
        "https://api.deepseek.com/v1",
        lambda s: s.deepseek_api_key,
    ),
    EndpointName.BAILIAN: Endpoint(
        EndpointName.BAILIAN,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        lambda s: s.bailian_api_key,
    ),
}


# —— 第 3 层：模型档（family = 模型名前缀，是这层的身份 / 主键）——
@dataclass(frozen=True)
class ModelProfile:
    """模型族声明：family + transport + 能力 flag。

    - `family`: 模型名前缀（如 `"deepseek-v4"`），命中 `-flash` / `-pro` 等变体；
      是本层唯一主键（不再与 dict key 重复）
    - `transport`: 由模型档唯一决定，结构上不可能再分叉（消除陷阱①
      「同 (bailian, deepseek-v4) 因 role 不同被 ChatDeepSeek / ChatOpenAI
      两种 client 触达」）
    - `supports_reasoning`: 是否走 reasoning_content 提取（Step 5 解耦用）
    - `supports_tools`: 是否支持 bind_tools
    - `multimodal`: 是否支持多模态 content
    """

    family: str
    transport: Transport
    supports_reasoning: bool
    supports_tools: bool
    multimodal: bool


# 用元组而非 dict：family 字段作单一来源，不再与 dict key 重复。
MODEL_PROFILES: tuple[ModelProfile, ...] = (
    ModelProfile("deepseek-v4", Transport.CHAT_DEEPSEEK, True, True, False),
    # ModelProfile("qwen-vl", Transport.CHAT_OPENAI, True, True, True),
    # 写 adapter + 真机探针后再启用（参考本模块 docstring「新族纪律」）
)


class ModelProfileNotRegisteredError(LookupError):
    """模型名无对应模型档时抛出（resolve_profile 未命中）。"""


def resolve_profile(model: str) -> ModelProfile:
    """按模型名前缀解析 ModelProfile。最长前缀优先，防族前缀重叠误命中。

    Raises:
        ModelProfileNotRegisteredError: 今日仅 deepseek-v4 一个族，所有未命中
            都会抛错；新族注册前禁止「先跑起来再说」。
    """
    matches = [p for p in MODEL_PROFILES if model.startswith(p.family)]
    if not matches:
        raise ModelProfileNotRegisteredError(
            f"模型 {model!r} 无对应模型档；新族需先写 adapter 并通过真机探针"
        )
    return max(matches, key=lambda p: len(p.family))


# —— 第 2 层：role 绑定 ——
@dataclass(frozen=True)
class RoleBinding:
    """role → (endpoint, model, 思考参数, fallback, 重试次数) 的不可变绑定。

    `model` 是具体模型名（开放数据），由 resolve_profile 在 Step 2 的
    `_build_binding` 入口校验该 model 有档。retry_attempts 是 application-level
    `with_retry(stop_after_attempt=...)` 的次数，不是 LangChain SDK retries。

    ⚠️ **fallback 自身的 retry_attempts 是死字段**：
    Step 3 的 `_build_role_llm` 只读顶层 `ROLES[role].retry_attempts`，
    `fallback` 绑定上的 retry_attempts（COMPRESSION 的 fallback 默认 = 3）
    永不被消费。Step 2/3 不可误读为「兜底也重试 3 次」。若未来要让兜底
    也有自己的重试预算，需把 retry_attempts 上提到 `_build_role_llm`
    显式读两遍（参见 plan #3 「crisis/redline 重锦到 main」注释）。
    """

    endpoint: EndpointName
    model: str
    thinking: bool
    reasoning_effort: ReasoningEffort | None
    temperature: float | None
    fallback: RoleBinding | None = None
    retry_attempts: int = 3


# 默认模型常量（统一名，避免散落字符串）
_DSV4 = "deepseek-v4-flash"

# crisis / redline 不在此处单列——它们复用 ROLES[Role.MAIN]
# （接替对话：流式 + 思考、不绑工具，行为同 main 而非 audit）
ROLES: dict[Role, RoleBinding] = {
    # main：deepseek→bailian 真兜底（修今日 fallback_provider="deepseek"
    # 同端点假兜底 bug）；thinking=true、effort=max，temperature=None 走服务端默认
    Role.MAIN: RoleBinding(
        EndpointName.DEEPSEEK,
        _DSV4,
        True,
        ReasoningEffort.MAX,
        None,
        fallback=RoleBinding(
            EndpointName.BAILIAN,
            _DSV4,
            True,
            ReasoningEffort.MAX,
            None,
        ),
    ),
    # audit：同 main（main/audit 的 model 与思考参数今日恰好等价）
    Role.AUDIT: RoleBinding(
        EndpointName.DEEPSEEK,
        _DSV4,
        True,
        ReasoningEffort.MAX,
        None,
        fallback=RoleBinding(
            EndpointName.BAILIAN,
            _DSV4,
            True,
            ReasoningEffort.MAX,
            None,
        ),
    ),
    # compression：thinking 关闭、temperature=0.3（保稳定），retry_attempts=1
    # （Iver 拍板：避免后台压缩在主端抖动时放大重试，保留跨端兜底）
    # ⚠️ 其 fallback 的 retry_attempts=3 是死字段（见 RoleBinding 注释）
    Role.COMPRESSION: RoleBinding(
        EndpointName.DEEPSEEK,
        _DSV4,
        False,
        None,
        0.3,
        fallback=RoleBinding(
            EndpointName.BAILIAN,
            _DSV4,
            False,
            None,
            0.3,
        ),
        retry_attempts=1,
    ),
}
