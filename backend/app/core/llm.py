"""LLM provider 装配：role 驱动入口 + 三层正交拓扑（Step 3 重构后）。

Step 3 删除的旧符号：
  - `_PROVIDER_REGISTRY` / `_PUBLIC_KEYS` / `_parse_key` /
    `_CLIENT_BUILDER` / `_MODEL_FIELD` / `_ROLE_SETTINGS` /
    `_build_chat_deepseek`（旧） / `_build_compression_deepseek`
  - 历史偶然的 `f"audit_{main_provider}"` 字符串拼接
    （crisis / redline 复用 Role.MAIN 绑定，不另走 audit）
  - 旧 `_test_llm_overrides: dict[str, Runnable]` 字符串键

Step 3 新增的入口（皆经 `_build_binding` = Step 2 装配链）：
  - `build_role_primary(role, settings)` / `build_role_fallback(role, settings)`
  - `wrap_resilience(primary, fallback, *, retry_attempts=3)`
  - `_build_role_llm(role, settings)` — retry 次数取 `ROLES[role].retry_attempts`
  - `build_compression_llm(settings)` — pipeline.py 压缩调用点
  - `build_main_llm` / `build_crisis_llm` / `build_redline_llm`
    全部经 `_build_role_llm(Role.MAIN, ...)`,crisis/redline 复用 main 绑定
    （流式+思考、不绑工具,行为本同 main 而非 audit）

注入缝：
  - `_test_llm_overrides: dict[Role, Runnable]` 改 Role 枚举键
  - `set_test_llm(role: Role | str, llm)` / `clear_test_llm(role=None)`
    接受 `Role` 或旧字符串（旧字符串经 `_legacy_provider_to_role`
    归一到 Role;`build_role_fallback` 不查 override,保持「主端 fake / 备端
    real」语义不变）—— Step 7 删字符串分支

shim 留存:
  - `build_provider_llm(provider, settings)` 降级为 back-compat shim,路由
    "deepseek" / "audit_deepseek" 到 `build_role_primary`、"audit_bailian"
    到 `build_role_fallback`,「openai」/「compression_deepseek」抛
    `ProviderNotRegisteredError`。Step 6 重写 `audit/llm.py` 时同步删 shim。
  - `_build_chat_openai` 按计划「暂留给未来或移走」取暂留分支,无生产调用方。

构建链:`role → (endpoint, model) → 模型档给 transport+方言 → 实例化`。

monkeypatch 段(M8-hotfix):langchain-openai _convert_message_to_dict 序列化
AIMessage 时保留 reasoning_content,DeepSeek 思考模式下 tool_calls 后续请求
不会因 400 报错。详见 LLM Provider 探针 补4。Step 3 逐字未动。
"""

from __future__ import annotations

import importlib.metadata as _metadata
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from app.core.llm_topology import (
    ENDPOINTS,
    LLM_REQUEST_TIMEOUT_SECONDS,
    ROLES,
    Role,
    RoleBinding,
    Transport,
    resolve_profile,
)

if TYPE_CHECKING:
    from app.core.config import Settings

# ---- M8-hotfix: _convert_message_to_dict monkeypatch for reasoning_content ----
# 背景：langchain-openai 的 _convert_message_to_dict 序列化 AIMessage 时，
# 不会将 additional_kwargs.reasoning_content 传给 OpenAI API。DeepSeek 思考模式
# 要求：做过 tool_calls 的轮次后续请求必须回传 reasoning_content，否则 API 返回 400。
# 详见 LLM Provider 探针补4 多轮 agentic 用例。
# TODO: langchain-deepseek upstream PR 合入后移除本 monkeypatch。

_VERIFIED_LCO_VERSIONS = ("1.2.",)  # 当前已验证版本前缀
_lco_version = _metadata.version("langchain-openai")
assert any(_lco_version.startswith(v) for v in _VERIFIED_LCO_VERSIONS), (
    f"langchain-openai 版本 {_lco_version} 未经验证，"
    f"_convert_message_to_dict monkeypatch 可能失效。"
    f"已验证版本前缀：{_VERIFIED_LCO_VERSIONS}。"
    f"升级版本前请重新跑 LLM Provider 探针的补4 用例。"
)

import langchain_openai.chat_models.base as _lcoai  # noqa: E402 — 必须 at 顶部之后（模块级副作用）

assert hasattr(_lcoai, "_convert_message_to_dict"), (
    "langchain_openai.chat_models.base._convert_message_to_dict 不存在，"
    "monkeypatch 失败。请检查 langchain-openai API 是否有变更。"
)

_orig_convert = _lcoai._convert_message_to_dict


def _patched_convert(message, *args, **kwargs):
    """补 langchain-openai 序列化时丢失 reasoning_content 的缺陷。

    DeepSeek 思考模式下，做过 tool_calls 的轮次后续请求必须回传
    reasoning_content，否则 API 返回 400。
    详见 LLM Provider 探针 补4。

    使用 *args, **kwargs 透传以兼容 LangChain 内部可能的位置参数调用。
    """
    result = _orig_convert(message, *args, **kwargs)
    if isinstance(message, AIMessage):
        rc = (message.additional_kwargs or {}).get("reasoning_content")
        if rc:
            result["reasoning_content"] = rc
    return result


_lcoai._convert_message_to_dict = _patched_convert
# ---- end monkeypatch ----


class ProviderNotRegisteredError(LookupError):
    """Provider 名未注册时抛出（仅 back-compat shim 内部使用）。"""


# ============================================================================
# Step 2 · adapter 层（transport 由模型档分发）
# ============================================================================
# 新路径:RoleBinding → 查 ENDPOINTS + resolve_profile → dispatch 到 transport adapter。
# Step 3 在此基础上加 role 维度入口(build_role_primary / build_role_fallback /
# wrap_resilience / _build_role_llm / build_compression_llm)。
#
# 关键不变量(关注点 1 语义等价):
# - main / audit 的 thinking=True & reasoning_effort=MAX 与今日 settings 默认值一致
# - compression 的 thinking=False & temperature=0.3 与今日 _build_compression_deepseek 一致

_TransportBuilder = Callable[[str, str, RoleBinding], BaseChatModel]
"""transport adapter 签名:取 (api_key, base_url, RoleBinding) 返回 chat model 实例。"""


def _adapter_chat_deepseek(
    api_key: str,
    base_url: str,
    b: RoleBinding,
) -> BaseChatModel:
    """deepseek-v4 族 adapter:从 RoleBinding 装配 ChatDeepSeek 实例。

    与旧 _build_chat_deepseek 区别:reasoning_effort 与 temperature 降为可选。
    - `reasoning_effort=None` 时不塞 extra_body(compression 角色走此路径)
    - `temperature=None` 时不传该 kwarg(让 ChatDeepSeek 走服务端默认)
    """
    extra_body: dict[str, Any] = {
        "thinking": {"type": "enabled" if b.thinking else "disabled"},
    }
    if b.reasoning_effort is not None:
        # StrEnum .value 拿裸字符串字面量(不走 str(e) 隐式路径,llm_topology.py L89-91 注释明示)
        extra_body["reasoning_effort"] = b.reasoning_effort.value
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "api_base": base_url,
        "model": b.model,
        "timeout": LLM_REQUEST_TIMEOUT_SECONDS,
        "max_retries": 0,  # SDK 内置重试关掉;wrap_resilience 统一管
        "extra_body": extra_body,
    }
    if b.temperature is not None:
        kwargs["temperature"] = b.temperature
    return ChatDeepSeek(**kwargs)


# 键 = Transport 枚举;只注册已实现的 transport。CHAT_OPENAI / CHAT_TONGYI
# 占位枚举不注册——任何 ModelProfile 引用未实现 transport 会在 _TRANSPORTS[T]
# 查找中抛 KeyError。新族纪律:写 adapter + 真机探针后再启用。
_TRANSPORTS: dict[Transport, _TransportBuilder] = {
    Transport.CHAT_DEEPSEEK: _adapter_chat_deepseek,
}


def _build_binding(b: RoleBinding, settings: Settings) -> BaseChatModel:
    """RoleBinding → ChatModel 实例的装配入口(Step 2 引入,Step 3 复用)。

    装配链:role → (endpoint, model) → 模型档给 transport+方言 → 实例化。
    抛 ModelProfileNotRegisteredError:model 名无模型档(防新族未探针先上线)。
    """
    ep = ENDPOINTS[b.endpoint]
    profile = resolve_profile(b.model)
    api_key = ep.api_key(settings).get_secret_value()
    return _TRANSPORTS[profile.transport](api_key, ep.base_url, b)


# ============================================================================
# Step 3 · role 驱动入口
# ============================================================================
# 装配顺序:
#   build_role_primary / build_role_fallback → _build_binding(裸 ChatModel 实例)
#   wrap_resilience(primary, fallback, retry) → primary.with_retry(...).with_fallbacks([fallback])
#   _build_role_llm(role, settings) → 串起 primary + fallback + wrap,
#     retry 取 ROLES[role].retry_attempts
# 注入缝:build_role_primary 优先返回 _test_llm_overrides[role] 的 override;
#        build_role_fallback 不查 override,保持「主端 fake / 备端 real」语义


def build_role_primary(role: Role, settings: Settings) -> BaseChatModel:
    """role 主端 LLM 实例(裸 ChatModel,未包 retry / fallback)。

    注入缝:若 `role in _test_llm_overrides`,直接返回 override(短路 _build_binding)。

    抛:
        ModelProfileNotRegisteredError: `ROLES[role].model` 无对应模型档。
    """
    if role in _test_llm_overrides:
        return _test_llm_overrides[role]
    return _build_binding(ROLES[role], settings)


def build_role_fallback(role: Role, settings: Settings) -> BaseChatModel | None:
    """role 备端 LLM 实例(裸 ChatModel,未包 retry / fallback)。

    注入缝**不**查 override:今日约定「主端 override、备端真实」,
    若备端也走 override 则重试耗尽后无真实降级路径可用,违反 fail-safe。

    返回 None 时表示该 role 无 fallback 配置(今日三 role 全部有,此分支防御性)。
    """
    fb = ROLES[role].fallback
    if fb is None:
        return None
    return _build_binding(fb, settings)


def wrap_resilience(
    primary: Runnable,
    fallback: Runnable | None,
    *,
    retry_attempts: int = 3,
) -> Runnable:
    """主端 retry + 备端 fallback 包装。

    链式形态:`primary.with_retry(...).with_fallbacks([fallback])`。
    - retry 触发:RateLimitError / APITimeoutError / APIConnectionError
    - `retry_attempts=1`:仅 1 次尝试(不重试),失败后仍走 fallback
    - `retry_attempts>=2`:指数抖动 + 触发后退到 fallback
    - `fallback is None`:仅返回 retryable primary(不包 with_fallbacks)
    """
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    retryable = primary.with_retry(
        retry_if_exception_type=(RateLimitError, APITimeoutError, APIConnectionError),
        stop_after_attempt=retry_attempts,
        wait_exponential_jitter=True,
    )
    return retryable.with_fallbacks([fallback]) if fallback else retryable


def _build_role_llm(role: Role, settings: Settings) -> Runnable:
    """role 维度的 LLM 装配入口:primary + retry + fallback 一体化。

    retry 次数取 `ROLES[role].retry_attempts`(main / audit = 3,compression = 1)。
    返回 Runnable:含 retry 包装的 primary + 单一 fallback 的 RunnableWithFallbacks
    链;若 role 无 fallback(防御性)则仅返回 retryable primary。
    """
    return wrap_resilience(
        build_role_primary(role, settings),
        build_role_fallback(role, settings),
        retry_attempts=ROLES[role].retry_attempts,
    )


def build_main_llm(settings: Settings) -> Runnable:
    """主对话 LLM(role=MAIN):retry=3 + bailian 真兜底。

    Step 3 行为变化(主对话 happy-path 与今日 settings 默认值等价,见计划
    验收清单「行为边界」):
      - 旧 `fallback_provider="deepseek"` 假兜底(deepseek→deepseek 同端点重试)
        → 新 `ROLES[MAIN].fallback=bailian` 真冗余
      - 兜底端带 thinking + reasoning_effort=max,与主端等价(均走
        `_adapter_chat_deepseek` 同一 transport)
    """
    return _build_role_llm(Role.MAIN, settings)


def build_crisis_llm(settings: Settings) -> Runnable:
    """crisis 干预 LLM(role=MAIN,不复用 audit)。

    行为本同 main(接替对话:流式+思考、不绑工具),而非 audit note-writer。
    Step 3 行为变化(发现与建议 #6):
      - 旧 `build_provider_llm("audit_deepseek")` 裸实例(无 retry / 无 fallback)
        → 新 `_build_role_llm(Role.MAIN, ...)` 首次获得 retry=3 + bailian 兜底
      - happy-path 调用零行为变化(今日 main/audit 的 model+effort 恰好等价)
      - 错误路径有增益(plan #6:「验收须实测 crisis/redline 兜底真生效,勿假设零变化」)
    """
    return _build_role_llm(Role.MAIN, settings)


def build_redline_llm(settings: Settings) -> Runnable:
    """redline 干预 LLM(role=MAIN,不复用 audit)。行为同 build_crisis_llm。"""
    return _build_role_llm(Role.MAIN, settings)


def build_compression_llm(settings: Settings) -> Runnable:
    """压缩 LLM(role=COMPRESSION):retry=1 + bailian 兜底。

    Step 3 行为变化(plan 发现与建议 #2 Iver 拍板):
      - 旧 `build_provider_llm("compression_deepseek")` 裸实例(无 retry / 无 fallback)
        → 新 `_build_role_llm(Role.COMPRESSION, ...)` 首次获得 retry=1 + bailian 兜底
      - retry=1:仅 1 次尝试、不重试,避免后台压缩在主端抖动时放大重试,
        仍保留跨端兜底(主端 1 次失败即切 bailian)
      - pipeline.py:144-147 调用点同步切换到本函数
    """
    return _build_role_llm(Role.COMPRESSION, settings)


# ============================================================================
# 集成测试注入缝（Step 3 改 Role 键）
# ============================================================================
# 旧 key 字符串经 `_legacy_provider_to_role` 归一到 Role 枚举后写入;
# build_role_primary 优先返回 override,build_role_fallback 不查(语义不变)。
# Step 7 删 `_legacy_provider_to_role` 字符串分支,全量改 Role 枚举键。

_test_llm_overrides: dict[Role, BaseChatModel] = {}


def _legacy_provider_to_role(provider: str) -> Role:
    """旧公开 key 字符串 → Role 枚举归一(Step 7 删)。

    - "deepseek" / "openai" → Role.MAIN(openai 今日是 bailian 端点的主别名,新结构已删)
    - "audit" / "audit_*" → Role.AUDIT
    - "compression" / "compression_*" → Role.COMPRESSION

    ⚠️ Role 是 StrEnum 子类,`isinstance(Role.MAIN, str) is True` —— 入口归一
    路径要正确处理「裸 role value」与「带前缀旧 key」两类输入。
    """
    if provider == "audit" or provider.startswith("audit_"):
        return Role.AUDIT
    if provider == "compression" or provider.startswith("compression_"):
        return Role.COMPRESSION
    return Role.MAIN


def set_test_llm(role: Role | str, llm: BaseChatModel) -> None:
    """设置指定 role 的测试用 LLM 实例(注入缝)。

    `role` 接受 `Role` 枚举或旧公开 key 字符串(经 `_legacy_provider_to_role`
    归一);设入后 `build_role_primary(role, ...)` 返回此实例(短路 _build_binding)。
    `build_role_fallback` 不读此 override(语义「主端 fake / 备端 real」)。
    调用 `clear_test_llm()` 恢复生产行为。

    参数:
        llm 必须是 `BaseChatModel` 实例(与生产路径 `_build_binding` 返回类型
            一致),允许测试用 mock `__init__` 桩造的 ChatDeepSeek。
    """
    normalized = _legacy_provider_to_role(role) if isinstance(role, str) else role
    _test_llm_overrides[normalized] = llm


def clear_test_llm(role: Role | str | None = None) -> None:
    """清除测试 LLM override。

    `role=None` 时清除全部;否则仅清除指定 role(字符串经归一)。
    """
    if role is None:
        _test_llm_overrides.clear()
        return
    normalized = _legacy_provider_to_role(role) if isinstance(role, str) else role
    _test_llm_overrides.pop(normalized, None)


# ============================================================================
# Back-compat shim:build_provider_llm(Step 6 重写 audit/llm.py 时删)
# ============================================================================
# 旧公开 key 字符串路由:
#   "deepseek" / "audit_deepseek" → build_role_primary(role)
#   "audit_bailian"               → build_role_fallback(Role.AUDIT)
#   "openai" / "compression_deepseek" / 未知 → 抛 ProviderNotRegisteredError
# 注入缝(字符串)经 `_legacy_provider_to_role` 归一后查 _test_llm_overrides,
# 命中则直接返回(对应 build_role_primary 的入口短路)。
# 关键约束:shim **不**走 `_build_role_llm` — 必须返回裸实例以让
# `audit/llm.py` 继续 `.bind_tools(...)`(RunnableWithFallbacks 无 bind_tools,
# 会 AttributeError;见 plan 关注点 3)。


def build_provider_llm(provider: str, settings: Any) -> BaseChatModel:
    """Back-compat shim:旧公开 key 字符串 → 裸 ChatModel 实例(未包 retry / fallback)。

    仅供 `app/domain/audit/llm.py:35-36` 现消费使用,Step 6 重写 audit/llm.py
    时同步删除。**不**走 `_build_role_llm`(后者返回 RunnableWithFallbacks,
    `audit/llm.py` 后续 `bind_tools` 会 AttributeError)。

    接受:"deepseek" / "audit_deepseek" / "audit_bailian"(配合 set_test_llm 注入缝)
    拒绝:"openai" / "compression_deepseek" / 未知(抛 ProviderNotRegisteredError)

    Raises:
        ProviderNotRegisteredError: provider 不在受支持集合。
    """
    # 注入缝:旧字符串归一后查 _test_llm_overrides
    role = _legacy_provider_to_role(provider)
    if role in _test_llm_overrides:
        return _test_llm_overrides[role]

    if provider == "audit_bailian":
        # audit_bailian 在旧字符串表里是 AUDIT role 的 fallback(不是 primary)
        fallback = build_role_fallback(Role.AUDIT, settings)
        if fallback is None:
            msg = f"Provider '{provider}' 无可用 fallback"
            raise ProviderNotRegisteredError(msg)
        return fallback

    if provider in ("deepseek", "audit_deepseek"):
        return build_role_primary(role, settings)

    # 旧 "openai"(ChatOpenAI 走 bailian 端点)与 "compression_deepseek"
    # 在新结构已删除 — 主对话 / 审查 / 压缩改走 build_main_llm /
    # build_audit_llm / build_compression_llm 入口
    msg = (
        f"Provider '{provider}' 不再受 build_provider_llm 支持；"
        f"主对话用 build_main_llm(settings) / 审查用 build_audit_llm / "
        f"压缩用 build_compression_llm(settings)。"
    )
    raise ProviderNotRegisteredError(msg)


# ============================================================================
# 旧 _build_chat_openai(计划「暂留给未来或移走」取暂留分支)
# ============================================================================
# 无生产调用方,ChatOpenAI 走 bailian 端点的旧路径今日无 role 绑定使用;
# 保留以便未来 qwen-vl / tongyi 等 OpenAI 兼容族落 adapter 时直接复用。
# ruff 不报 F401(私有函数豁免),`ChatOpenAI` import 因此保留。


def _build_chat_openai(
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
) -> ChatOpenAI:
    """Construct ChatOpenAI (no thinking — reserved for M11+ experiments)."""
    return ChatOpenAI(
        api_key=api_key,  # type: ignore[arg-type]
        base_url=base_url,
        model=model,
        timeout=timeout,
        max_retries=0,
    )


# ============================================================================
# Step 7 收口 stub:`_PROVIDER_REGISTRY` 空 dict 占位
# ============================================================================
# test_factory.py 顶部 `from app.core.llm import _PROVIDER_REGISTRY` 仍存在,
# 删除该符号会触发 ImportError 阻断 pytest 收集。最简兼容:提供空 dict 占位,
# test_factory.py 顶部 import 通过,内部断言(`_PROVIDER_REGISTRY["deepseek"]`)
# KeyError → 标记为"Step 7 整体重写 test_factory.py 时删本 stub"。

_PROVIDER_REGISTRY: dict[str, Any] = {}
"""Step 7 删:旧 _PROVIDER_REGISTRY 已由 `build_provider_llm` shim 取代,
本空 dict 占位仅为兼容 test_factory.py 顶部 import,不允许新代码引用。"""
