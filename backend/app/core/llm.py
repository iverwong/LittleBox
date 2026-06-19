"""LLM provider 装配：role 驱动入口 + 三层正交拓扑。

构建链：`role → (endpoint, model) → 模型档给 transport+方言 → 实例化`。

模块结构（自顶向下）：
  1. monkeypatch 段：补 langchain-openai 序列化 DeepSeek `reasoning_content` 的缺陷
  2. Transport adapter 层：把 `(api_key, base_url, RoleBinding)` 装配成具体 ChatModel
     （目前仅 deepseek 已实装；openai / tongyi 是枚举值但未注册 adapter）
  3. Binding 装配：`RoleBinding → ChatModel` 的统一入口（`_build_binding`）
  4. 测试注入缝（状态）：`_test_llm_overrides` 字典（`Role → BaseChatModel`）
  5. Role 装配 + 韧性：`build_role_primary` / `build_role_fallback` /
     `wrap_resilience` / `_build_role_llm`
  6. 公共入口：`build_main_llm` / `build_crisis_llm` / `build_compression_llm`
  7. 测试注入缝（API）：`set_test_llm` / `clear_test_llm`，仅接受 `Role` 枚举
  8. 保留 `_build_chat_openai`：当前无生产调用，留给未来 OpenAI 兼容族 adapter

crisis 行为本同 main（流式 + 思考、不绑工具），复用 `Role.MAIN` 绑定；
audit note-writer 走独立 `Role.AUDIT` 绑定。

注入缝纪律：
- `_test_llm_overrides` 键为 `Role` 枚举；`set_test_llm` / `clear_test_llm`
  显式 `isinstance` 守卫拒绝字符串 key，防「漏改调用 → 静默 override
  失效 → 回落真 LLM 难诊断」暗坑。
- `build_role_primary` 查 override，`build_role_fallback` **不**查
  （语义「主端 fake / 备端 real」：重试耗尽后仍有真实降级路径，fail-safe）。

保留分支（monkeypatch + `_build_chat_openai`）需「全留」或「全清」整组
决策，独立 scoped PR 处理。OpenAI 兼容族（qwen-vl / tongyi 等）真正落
adapter 时，两者一并复用。
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


# ============================================================================
# langchain-openai monkeypatch
# ============================================================================
# 背景：langchain-openai 的 `_convert_message_to_dict` 序列化 `AIMessage` 时，
# 不会将 `additional_kwargs.reasoning_content` 传给 OpenAI API。DeepSeek 思考
# 模式要求：做过 tool_calls 的轮次后续请求必须回传 reasoning_content，否则
# API 返回 400。
#
# 模块级副作用：必须在 `app.core.llm` import 时生效（测试侧
# `test_factory_monkeypatch` 与 `test_multiround_reasoning` 强依赖此时序假设）。
# 一旦 langchain-deepseek 上游 PR 合入，可移除本 monkeypatch。

_VERIFIED_LCO_VERSIONS = ("1.2.", "1.3.")  # 已验证版本前缀
_lco_version = _metadata.version("langchain-openai")
assert any(_lco_version.startswith(v) for v in _VERIFIED_LCO_VERSIONS), (
    f"langchain-openai 版本 {_lco_version} 未经验证,"
    f"_convert_message_to_dict monkeypatch 可能失效。"
    f"已验证版本前缀:{_VERIFIED_LCO_VERSIONS}。"
    f"升级版本前请重新跑 LLM Provider 探针的多轮 reasoning 用例。"
)

import langchain_openai.chat_models.base as _lcoai  # noqa: E402 — 必须 at 顶部之后(模块级副作用)

assert hasattr(_lcoai, "_convert_message_to_dict"), (
    "langchain_openai.chat_models.base._convert_message_to_dict 不存在,"
    "monkeypatch 失败。请检查 langchain-openai API 是否有变更。"
)

_orig_convert = _lcoai._convert_message_to_dict


def _patched_convert(message, *args, **kwargs):
    """补 langchain-openai 序列化时丢失 `reasoning_content` 的缺陷。

    DeepSeek 思考模式下，做过 tool_calls 的轮次后续请求必须回传
    `reasoning_content`，否则 API 返回 400。使用 `*args, **kwargs` 透传
    以兼容 LangChain 内部可能的位置参数调用。
    """
    result = _orig_convert(message, *args, **kwargs)
    if isinstance(message, AIMessage):
        rc = (message.additional_kwargs or {}).get("reasoning_content")
        if rc:
            result["reasoning_content"] = rc
    return result


_lcoai._convert_message_to_dict = _patched_convert


# ============================================================================
# Transport adapter 层
# ============================================================================
# transport adapter 签名：取 `(api_key, base_url, RoleBinding)` 返回 chat model 实例。
# 新族纪律：写 adapter + 真机探针后再启用。
#
# 关键不变量：
# - main / audit 的 `thinking=True & reasoning_effort=MAX` 与 `Settings` 默认值一致；
# - compression 的 `thinking=False & temperature=0.3` 与生产历史一致。

_TransportBuilder = Callable[[str, str, RoleBinding], BaseChatModel]
"""transport adapter 签名：取 `(api_key, base_url, RoleBinding)` 返回 chat model 实例。"""


def _adapter_chat_deepseek(
    api_key: str,
    base_url: str,
    b: RoleBinding,
) -> BaseChatModel:
    """deepseek-v4 族 adapter：从 `RoleBinding` 装配 `ChatDeepSeek` 实例。

    可选 kwarg 语义：
    - `reasoning_effort=None` 时不塞 `extra_body`（`compression` 角色走此路径）；
    - `temperature=None` 时不传该 kwarg（让 `ChatDeepSeek` 走服务端默认）。

    `max_retries=0` 把 SDK 内置重试关掉，改由 `wrap_resilience` 统一管。

    Args:
        api_key: provider API key。
        base_url: 端点 base URL。
        b: role 绑定。

    Returns:
        配置完成的 `ChatDeepSeek` 实例。
    """
    extra_body: dict[str, Any] = {
        "thinking": {"type": "enabled" if b.thinking else "disabled"},
    }
    if b.reasoning_effort is not None:
        # `StrEnum.value` 拿裸字符串字面量(不走 str(e) 隐式路径)
        extra_body["reasoning_effort"] = b.reasoning_effort.value
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "api_base": base_url,
        "model": b.model,
        "timeout": LLM_REQUEST_TIMEOUT_SECONDS,
        "max_retries": 0,
        "extra_body": extra_body,
    }
    if b.temperature is not None:
        kwargs["temperature"] = b.temperature
    return ChatDeepSeek(**kwargs)


# 键 = Transport 枚举；只注册已实现的 transport。CHAT_OPENAI / CHAT_TONGYI
# 是枚举值但未注册 adapter——任何 `ModelProfile` 引用未实现 transport 会在
# `_TRANSPORTS[T]` 查找中抛 `KeyError`。
_TRANSPORTS: dict[Transport, _TransportBuilder] = {
    Transport.CHAT_DEEPSEEK: _adapter_chat_deepseek,
}


# ============================================================================
# Binding 装配
# ============================================================================


def _build_binding(b: RoleBinding, settings: Settings) -> BaseChatModel:
    """`RoleBinding → ChatModel` 实例的统一装配入口。

    装配链：`role → (endpoint, model) → 模型档给 transport+方言 → 实例化`。

    Args:
        b: role 绑定。
        settings: 全局配置（提供 API key 读取入口）。

    Returns:
        配置完成的 chat model 实例。

    Raises:
        ModelProfileNotRegisteredError: `b.model` 无对应模型档。
    """
    ep = ENDPOINTS[b.endpoint]
    profile = resolve_profile(b.model)
    api_key = ep.api_key(settings).get_secret_value()
    return _TRANSPORTS[profile.transport](api_key, ep.base_url, b)


# ============================================================================
# 测试注入缝(状态)
# ============================================================================
# 状态变量提前到使用它的 `build_role_primary` 之前，避免前向引用。
# 配套的 API 函数 `set_test_llm` / `clear_test_llm` 见文件后半「测试注入缝(API)」段。
# 注入语义：`build_role_primary` 查 override；`build_role_fallback` 不查
# （主端 fake / 备端 real，重试耗尽后仍有真实降级，fail-safe）。

_test_llm_overrides: dict[Role, BaseChatModel] = {}


# ============================================================================
# Role 装配 + 韧性
# ============================================================================
# 装配顺序：
#   build_role_primary / build_role_fallback → _build_binding(裸 ChatModel 实例)
#   wrap_resilience(primary, fallback, retry) → primary.with_retry(...).with_fallbacks([fallback])
#   _build_role_llm(role, settings) → 串起 primary + fallback + wrap,
#     retry 取 ROLES[role].retry_attempts


def build_role_primary(role: Role, settings: Settings) -> BaseChatModel:
    """role 主端 LLM 实例（裸 ChatModel，未包 retry / fallback）。

    注入缝：若 `role in _test_llm_overrides`，直接返回 override
    （短路 `_build_binding`）。

    Args:
        role: 目标 role。
        settings: 全局配置。

    Returns:
        主端 chat model 实例。

    Raises:
        ModelProfileNotRegisteredError: `ROLES[role].model` 无对应模型档。
    """
    if role in _test_llm_overrides:
        return _test_llm_overrides[role]
    return _build_binding(ROLES[role], settings)


def build_role_fallback(role: Role, settings: Settings) -> BaseChatModel | None:
    """role 备端 LLM 实例（裸 ChatModel，未包 retry / fallback）。

    注入缝**不**查 override：约定「主端 fake / 备端 real」，若备端也走
    override 则重试耗尽后无真实降级路径可用，违反 fail-safe。

    Args:
        role: 目标 role。
        settings: 全局配置。

    Returns:
        备端 chat model 实例；当 role 无 fallback 配置时返回 `None`
        （今日三 role 全部有，此分支为防御性）。
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

    链式形态：`primary.with_retry(...).with_fallbacks([fallback])`。
    - 触发重试的异常：`RateLimitError` / `APITimeoutError` / `APIConnectionError`；
    - `retry_attempts=1`：仅 1 次尝试（不重试），失败后仍走 fallback；
    - `retry_attempts>=2`：指数抖动 + 触发后退到 fallback；
    - `fallback is None`：仅返回 retryable primary（不包 `with_fallbacks`）。

    Args:
        primary: 主端 Runnable。
        fallback: 备端 Runnable；为 `None` 时仅返回 retryable primary。
        retry_attempts: 主端 application-level 重试次数，默认 3。

    Returns:
        包装后的 Runnable。
    """
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    retryable = primary.with_retry(
        retry_if_exception_type=(RateLimitError, APITimeoutError, APIConnectionError),
        stop_after_attempt=retry_attempts,
        wait_exponential_jitter=True,
    )
    return retryable.with_fallbacks([fallback]) if fallback else retryable


def _build_role_llm(role: Role, settings: Settings) -> Runnable:
    """role 维度的 LLM 装配入口：primary + retry + fallback 一体化。

    retry 次数取 `ROLES[role].retry_attempts`（`main` / `audit` = 3，
    `compression` = 1）。返回含 retry 包装的 primary + 单一 fallback 的
    `RunnableWithFallbacks` 链；若 role 无 fallback（防御性）则仅返回
    retryable primary。

    Args:
        role: 目标 role。
        settings: 全局配置。

    Returns:
        Runnable 实例。
    """
    return wrap_resilience(
        build_role_primary(role, settings),
        build_role_fallback(role, settings),
        retry_attempts=ROLES[role].retry_attempts,
    )


# ============================================================================
# 公共入口
# ============================================================================
# crisis 复用 `Role.MAIN` 绑定（行为本同 main：流式+思考、不绑工具），
# 而非 audit note-writer。compression 独立 `Role.COMPRESSION` 绑定（无思考、
# temperature=0.3、retry=1）。


def build_main_llm(settings: Settings) -> Runnable:
    """主对话 LLM（`role=MAIN`）：retry=3 + bailian 真兜底。

    主端 deepseek thinking + reasoning_effort=max，备端 bailian 等价配置
    （均走 `_adapter_chat_deepseek` 同一 transport）。

    Args:
        settings: 全局配置。

    Returns:
        Runnable 实例。
    """
    return _build_role_llm(Role.MAIN, settings)


def build_crisis_llm(settings: Settings) -> Runnable:
    """crisis 干预 LLM（`role=MAIN`，不复用 audit）。

    行为本同 main（接替对话：流式 + 思考、不绑工具），而非 audit note-writer。
    retry=3 + bailian 兜底。

    Args:
        settings: 全局配置。

    Returns:
        Runnable 实例。
    """
    return _build_role_llm(Role.MAIN, settings)


def build_compression_llm(settings: Settings) -> Runnable:
    """压缩 LLM（`role=COMPRESSION`）：retry=1 + bailian 兜底。

    retry=1：仅 1 次尝试、不重试，避免后台压缩在主端抖动时放大重试，
    仍保留跨端兜底（主端 1 次失败即切 bailian）。
    调用点：`app/domain/chat/pipeline.py`。

    Args:
        settings: 全局配置。

    Returns:
        Runnable 实例。
    """
    return _build_role_llm(Role.COMPRESSION, settings)


# ============================================================================
# 测试注入缝(API)
# ============================================================================
# `set_test_llm` / `clear_test_llm` 仅接受 `Role` 枚举（运行时不依赖
# type annotation，显式 `isinstance` 守卫）。字符串 provider key 立即抛
# `TypeError`，防「漏改字符串调用 → 静默 override 失效 → 回落真 LLM 难诊断」
# 暗坑。状态变量 `_test_llm_overrides` 在文件前部「测试注入缝(状态)」段定义。


def set_test_llm(role: Role, llm: BaseChatModel) -> None:
    """设置指定 role 的测试用 LLM 实例（注入缝）。

    仅接受 `Role` 枚举。设入后 `build_role_primary(role, ...)` 返回此实例
    （短路 `_build_binding`）；`build_role_fallback` 不读此 override
    （语义「主端 fake / 备端 real」）。调用 `clear_test_llm()` 恢复生产行为。

    Args:
        role: 目标 `Role` 枚举。字符串立即抛 `TypeError`。
        llm: 测试用 `BaseChatModel` 实例（与生产路径 `_build_binding`
            返回类型一致），允许测试用 `__init__` mock 桩造的 `ChatDeepSeek`。

    Raises:
        TypeError: `role` 不是 `Role` 枚举。
    """
    if not isinstance(role, Role):
        raise TypeError(
            f"set_test_llm 仅接受 Role 枚举,实得 {type(role).__name__}={role!r};"
            '旧字符串 provider key("deepseek"/"audit_deepseek"/'
            '"compression_deepseek")已废,请用 Role.MAIN / '
            "Role.AUDIT / Role.COMPRESSION"
        )
    _test_llm_overrides[role] = llm


def clear_test_llm(role: Role | None = None) -> None:
    """清除测试 LLM override。

    仅接受 `Role` 枚举或 `None`；字符串输入立即抛 `TypeError`（与
    `set_test_llm` 对称）。`role=None` 时清除全部；否则仅清除指定 role。

    Args:
        role: 目标 `Role` 枚举；`None` 表示清空全部。

    Raises:
        TypeError: `role` 不是 `Role` 枚举或 `None`。
    """
    if role is not None and not isinstance(role, Role):
        raise TypeError(
            f"clear_test_llm 仅接受 Role 枚举或 None,实得 {type(role).__name__}={role!r};"
            "请用 Role.MAIN / Role.AUDIT / Role.COMPRESSION"
        )
    if role is None:
        _test_llm_overrides.clear()
        return
    _test_llm_overrides.pop(role, None)


# ============================================================================
# 保留：OpenAI 兼容族 adapter
# ============================================================================
# `_build_chat_openai` 当前无生产调用方，`ChatOpenAI` 走 bailian 端点的旧
# 路径今日无 role 绑定使用；保留以便未来 qwen-vl / tongyi 等 OpenAI 兼容族
# 落 adapter 时直接复用。ruff 不报 F401（私有函数豁免），顶 `ChatOpenAI`
# import 因此保留。同样原因，文件顶部 monkeypatch 段（给 langchain-openai
# 打补丁）整组保留，待 OpenAI 族真正启用时一并复用——两者「全留」或「全清」
# 整组决策。


def _build_chat_openai(
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
) -> ChatOpenAI:
    """构造 `ChatOpenAI`（无思考模式，保留给未来 OpenAI 兼容族实验）。

    Args:
        api_key: provider API key。
        base_url: 端点 base URL。
        model: 模型名。
        timeout: 超时秒数。

    Returns:
        配置完成的 `ChatOpenAI` 实例。
    """
    return ChatOpenAI(
        api_key=api_key,  # type: ignore[arg-type]
        base_url=base_url,
        model=model,
        timeout=timeout,
        max_retries=0,
    )
