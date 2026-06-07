"""LLM provider 装配:role × provider 二维工厂(D-7, Phase 4.1)。

5 条 _PROVIDER_REGISTRY key (deepseek / openai / audit_deepseek / audit_bailian /
compression_deepseek) 合并为二维表:
  - role 决定 model 字段 / thinking 字段 / reasoning_effort 字段 / temperature
  - (role, provider) 决定 client builder(陷阱 ①:同 provider 不同 role 可走不同 client)

陷阱 ①(实证):provider 决定的不只是 creds,还有 client 类。
  - main/openai(走 bailian 端点)→ ChatOpenAI,无 thinking
  - audit/bailian(走 bailian 端点)→ ChatDeepSeek,带 thinking + reasoning
  二者同样打 bailian 端点,但 client 类不同,所以 (role, provider) 二元映射必须显式。

陷阱 ②(实证):compression 角色走独立 _build_compression_deepseek,
  temperature=0.3,extra_body 只有 thinking、无 reasoning_effort。
  折叠为统一表时,compression 走 self-contained 分支,不混入 reasoning_effort。

monkeypatch 段(M8-hotfix):langchain-openai _convert_message_to_dict 序列化
AIMessage 时保留 reasoning_content,DeepSeek 思考模式下 tool_calls 后续请求
不会因 400 报错。详见 LLM Provider 探针 补4。
"""

from __future__ import annotations

import importlib.metadata as _metadata
from typing import Any, Callable

from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

# ---- M8-hotfix: _convert_message_to_dict monkeypatch for reasoning_content ----
# 背景：langchain-openai 的 _convert_message_to_dict 序列化 AIMessage 时，
# 不会将 additional_kwargs.reasoning_content 传给 OpenAI API。DeepSeek 思考模式
# 要求：做过 tool_calls 的轮次后续请求必须回传 reasoning_content，否则 API 返回 400。
# 详见 LLM Provider 探针补4 多轮 agentic 用例。
# TODO: langchain-deepseek upstream PR 合入后移除本 monkeypatch。

_VERIFIED_LCO_VERSIONS = ("1.2.",)  # 当前已验证版本前缀
_lco_version = _metadata.version("langchain-openai")
assert any(
    _lco_version.startswith(v) for v in _VERIFIED_LCO_VERSIONS
), (
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
    """Provider 名未注册时抛出。"""


def _build_chat_deepseek(
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
    reasoning_effort: str,
    thinking_enabled: bool = True,
) -> ChatDeepSeek:
    """Construct ChatDeepSeek with thinking mode enabled.

    Note: ChatDeepSeek has its own ``api_base`` field (separate from
    ``BaseChatOpenAI.openai_api_base`` aliased from ``base_url``).
    The underlying OpenAI client reads ``api_base``, NOT ``openai_api_base``,
    so we pass ``api_base`` instead of ``base_url``.

    ``thinking_enabled`` (added M8 Step 4): controls whether ``extra_body``
    sets ``thinking.type=enabled`` or ``disabled``. Default ``True`` for
    backward compatibility with existing callers.
    """
    return ChatDeepSeek(
        api_key=api_key,  # type: ignore[arg-type]
        api_base=base_url,
        model=model,
        timeout=timeout,
        max_retries=0,  # SDK 内置重试关掉；统一由 with_retry 在应用层管理
        extra_body={
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
            "reasoning_effort": reasoning_effort,
        },
    )


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
        max_retries=0,  # SDK 内置重试关掉；统一由 with_retry 在应用层管理
    )


def _build_compression_deepseek(settings: Any) -> ChatDeepSeek:
    """压缩调用专用 DeepSeek 实例。thinking 默认关闭（compression_thinking_enabled=False）。

    独立 builder:extra_body 只有 thinking,无 reasoning_effort;temperature=0.3
    (陷阱 ② — 不能折叠进统一 _build_chat_deepseek,否则会多塞 reasoning_effort)。
    """
    return ChatDeepSeek(
        model=settings.compression_model,
        api_key=settings.deepseek_api_key.get_secret_value(),  # type: ignore[arg-type]
        api_base=settings.deepseek_base_url,
        timeout=settings.llm_request_timeout_seconds,
        max_retries=0,
        temperature=0.3,
        extra_body={
            "thinking": {
                "type": "enabled" if settings.compression_thinking_enabled else "disabled",
            },
        },
    )


# role 维度配置:(model_field, thinking_field, reasoning_effort_field)
# reasoning_effort_field=None 表示该 role 不传 reasoning_effort(走独立 builder)
_ROLE_SETTINGS: dict[str, tuple[str, str, str | None]] = {
    "main": ("deepseek_model", "main_thinking_enabled", "main_reasoning_effort"),
    "audit": ("audit_model", "audit_thinking_enabled", "audit_reasoning_effort"),
    "compression": ("compression_model", "compression_thinking_enabled", None),
}

# (role, provider) → client builder(陷阱 ①:同 provider 不同 role 可走不同 client)
_CLIENT_BUILDER: dict[tuple[str, str], Callable[..., Runnable]] = {
    ("main", "deepseek"): _build_chat_deepseek,
    ("main", "openai"): _build_chat_openai,
    ("audit", "deepseek"): _build_chat_deepseek,
    ("audit", "bailian"): _build_chat_deepseek,  # bailian 端点 + ChatDeepSeek client(陷阱 ①)
    ("compression", "deepseek"): _build_compression_deepseek,  # 独立 builder(陷阱 ②)
}

# 公开 key 列表(保持字符串名不变,调用方 (audit/llm.py / chat/graph.py / set_test_llm inject) 不改 key)
_PUBLIC_KEYS: tuple[str, ...] = (
    "deepseek",
    "openai",
    "audit_deepseek",
    "audit_bailian",
    "compression_deepseek",
)


def _parse_key(provider: str) -> tuple[str, str]:
    """从公开 key 拆出 (role, provider_base)。

    audit_*     → role='audit',  provider_base=*（如 audit_bailian → ('audit', 'bailian')）
    compression_* → role='compression', provider_base=*
    其他        → role='main',   provider_base=provider
    """
    if provider.startswith("audit_"):
        return ("audit", provider[len("audit_"):])
    if provider.startswith("compression_"):
        return ("compression", provider[len("compression_"):])
    return ("main", provider)


# ---- 集成测试注入缝（M9.5） ----
# 允许测试按 provider 名 override LLM 实例。
# 主图 LLM 用 provider="deepseek"（build_main_llm 调用），
# 审查图 LLM 用 provider="audit_deepseek"（build_audit_llm 调用），
# 可分别编排不同输出以实现阶段二路由验证。
# 注：本 override 在 build_provider_llm 层生效，影响所有经过此函数的调用链。
_test_llm_overrides: dict[str, Runnable] = {}


def set_test_llm(provider: str, llm: Runnable) -> None:
    """设置指定 provider 的测试用 LLM 实例。

    provider 名与 _PUBLIC_KEYS 成员一致（deepseek / openai / audit_deepseek / ...）。
    设入后所有调用 build_provider_llm(provider, ...) 均返回此实例。
    调用 clear_test_llm() 恢复生产行为。
    """
    _test_llm_overrides[provider] = llm


def clear_test_llm(provider: str | None = None) -> None:
    """清除测试 LLM override。

    provider=None 时清除全部 override；否则仅清除指定 provider。
    """
    if provider is None:
        _test_llm_overrides.clear()
    else:
        _test_llm_overrides.pop(provider, None)


def build_provider_llm(provider: str, settings: Any) -> Runnable:
    """Build a single LLM instance for the given provider name.

    Raises:
        ProviderNotRegisteredError: if provider is not in _PUBLIC_KEYS.
    """
    # 集成测试注入缝：优先返回 override
    if provider in _test_llm_overrides:
        return _test_llm_overrides[provider]

    role, prov = _parse_key(provider)
    builder = _CLIENT_BUILDER.get((role, prov))
    if builder is None:
        msg = f"Unknown provider '{provider}'. Registered: {list(_PUBLIC_KEYS)}"
        raise ProviderNotRegisteredError(msg)

    # compression 角色走 self-contained 独立 builder（陷阱 ②:不传 reasoning_effort / 自己取 settings）
    if builder is _build_compression_deepseek:
        return builder(settings)

    # main / audit:role 决定 model / thinking / reasoning 字段;provider 决定 creds
    model_field, thinking_field, reasoning_field = _ROLE_SETTINGS[role]
    if prov == "deepseek":
        api_key = settings.deepseek_api_key.get_secret_value()  # type: ignore[arg-type]
        base_url = settings.deepseek_base_url
    else:  # bailian / openai(走 bailian creds)
        api_key = settings.bailian_api_key.get_secret_value()  # type: ignore[arg-type]
        base_url = settings.bailian_base_url

    # _build_chat_openai 签名只有 (api_key, base_url, model, timeout),不开 thinking
    if builder is _build_chat_openai:
        return builder(
            api_key=api_key,
            base_url=base_url,
            model=getattr(settings, model_field),
            timeout=settings.llm_request_timeout_seconds,
        )

    # _build_chat_deepseek 需要 (api_key, base_url, model, timeout, thinking_enabled, reasoning_effort)
    return builder(
        api_key=api_key,
        base_url=base_url,
        model=getattr(settings, model_field),
        timeout=settings.llm_request_timeout_seconds,
        thinking_enabled=getattr(settings, thinking_field),
        reasoning_effort=getattr(settings, reasoning_field),  # type: ignore[arg-type]
    )


def build_main_llm(settings: Any) -> Runnable:
    """Build the main-chat LLM with optional fallback chain.

    When enable_fallback is True (default), returns a RunnableWithFallbacks:
      primary (settings.main_provider) + fallback (settings.fallback_provider).
    When False, returns the primary LLM instance directly (no fallback wrapper).

    Retry policy: 3 attempts with exponential jitter on RateLimitError,
    APITimeoutError, APIConnectionError (applied by with_retry).
    """
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    primary = build_provider_llm(settings.main_provider, settings)

    if not settings.enable_fallback or settings.fallback_provider is None:
        return primary

    secondary = build_provider_llm(settings.fallback_provider, settings)

    retryable = primary.with_retry(
        retry_if_exception_type=(RateLimitError, APITimeoutError, APIConnectionError),
        stop_after_attempt=3,
        wait_exponential_jitter=True,
    )
    return retryable.with_fallbacks([secondary])


def build_crisis_llm(settings: Any) -> Runnable:
    """crisis 干预 LLM：复用 audit_{main_provider} provider，不绑 tools。

    D2 决议：crisis 推理深度与 audit 一致（thinking=enabled + effort=max）。
    """
    return build_provider_llm(f"audit_{settings.main_provider}", settings)


def build_redline_llm(settings: Any) -> Runnable:
    """redline 干预 LLM：复用 audit_{main_provider} provider，不绑 tools。

    D2 决议：redline 推理深度与 audit 一致（thinking=enabled + effort=max）。
    """
    return build_provider_llm(f"audit_{settings.main_provider}", settings)


# ---- 向后兼容别名(测试 fixture)----
# 旧 _PROVIDER_REGISTRY 是 dict[str, Callable[[Settings], Runnable]],key 调 (settings) 返回 LLM。
# 新接口是 build_provider_llm(key, settings),为不破坏 test_factory.py 大量断言
# (T0/T1/T5/T5c 等,共 16+ 处),保留 _PROVIDER_REGISTRY 作为 5 个公开 key 的 lambda 桥接表。
# 这不是死代码:它被测试主动验证存在性 + callable 行为。
_PROVIDER_REGISTRY: dict[str, Callable[[Any], Runnable]] = {
    key: (lambda k: lambda s: build_provider_llm(k, s))(key) for key in _PUBLIC_KEYS
}
