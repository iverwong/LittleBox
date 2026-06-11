"""LLM provider 装配：role 驱动入口 + 三层正交拓扑（Step 3 重构,Step 7 收口）。

Step 3 删除的旧符号：
  - `_PROVIDER_REGISTRY` / `_PUBLIC_KEYS` / `_parse_key` /
    `_CLIENT_BUILDER` / `_MODEL_FIELD` / `_ROLE_SETTINGS` /
    `_build_chat_deepseek`（旧） / `_build_compression_deepseek`
  - 历史偶然的 `f"audit_{main_provider}"` 字符串拼接
    （crisis / redline 复用 Role.MAIN 绑定，不另走 audit）
  - 旧 `_test_llm_overrides: dict[str, Runnable]` 字符串键

Step 7 删除的 shim / 占位:
  - `build_provider_llm` back-compat shim(Step 3 引入,Step 7 收口删)
  - `ProviderNotRegisteredError` 异常类(仅 shim 内部使用,随 shim 删)
  - `_legacy_provider_to_role` 字符串归一 helper(仅 shim 内部使用)
  - `_PROVIDER_REGISTRY: dict = {}` 空 dict 占位(Step 7 整体重写 test_factory
    后无引用,占位本身失去意义,删)

Step 3 新增的入口（皆经 `_build_binding` = Step 2 装配链）：
  - `build_role_primary(role, settings)` / `build_role_fallback(role, settings)`
  - `wrap_resilience(primary, fallback, *, retry_attempts=3)`
  - `_build_role_llm(role, settings)` — retry 次数取 `ROLES[role].retry_attempts`
  - `build_compression_llm(settings)` — pipeline.py 压缩调用点
  - `build_main_llm` / `build_crisis_llm` / `build_redline_llm`
    全部经 `_build_role_llm(Role.MAIN, ...)`,crisis/redline 复用 main 绑定
    （流式+思考、不绑工具,行为本同 main 而非 audit）

注入缝（Step 7 收紧 Role 签名 + isinstance 守卫）：
  - `_test_llm_overrides: dict[Role, BaseChatModel]` 键为 Role 枚举
  - `set_test_llm(role: Role, llm)` / `clear_test_llm(role: Role | None)`
    仅接受 Role 枚举（旧字符串 provider key 立即抛 TypeError,防止
    「漏改字符串调用 → 静默 override 失效 → 回落真 LLM 难诊断」暗坑）

暂留分支（按 plan 一致保留,不在 Step 7 范围）:
  - `_build_chat_openai` 无生产调用方,留给未来 qwen-vl / tongyi 等 OpenAI
    兼容族落 adapter 时直接复用。配套 `from langchain_openai import ChatOpenAI`
    顶 import 因此保留(ruff F401 豁免)
  - monkeypatch 段(M8-hotfix):langchain-openai `_convert_message_to_dict` 补
    reasoning_content 序列化(DeepSeek 思考模式 tool_calls 多轮 400 防回)
  - 上述两者需「全留」或「全清」整组决策,独立 scoped PR 处理,不在本步范围

构建链:`role → (endpoint, model) → 模型档给 transport+方言 → 实例化`。
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
# 集成测试注入缝（Step 3 改 Role 键 + Step 7 收紧 Role 签名）
# ============================================================================
# `_test_llm_overrides` 键为 Role 枚举;build_role_primary 优先返回 override,
# build_role_fallback 不查(语义「主端 fake / 备端 real」不变)。
# Step 7 起 set_test_llm / clear_test_llm 仅接受 Role 枚举,旧字符串 provider key
# ("deepseek"/"audit_deepseek"/"compression_deepseek" 等)被显式 isinstance 守卫
# 拒绝,防止「漏改字符串调用 → 静默 override 失效 → 回落真 LLM 难诊断」暗坑。

_test_llm_overrides: dict[Role, BaseChatModel] = {}


def set_test_llm(role: Role, llm: BaseChatModel) -> None:
    """设置指定 role 的测试用 LLM 实例(注入缝)。

    仅接受 `Role` 枚举(运行时不依赖 type annotation,显式 isinstance 守卫)。
    旧字符串 provider key 已废:`set_test_llm("deepseek", x)` 立即抛 `TypeError`,
    防止「漏改字符串调用 → 静默 override 失效 → 回落真 LLM 难诊断」暗坑。

    设入后 `build_role_primary(role, ...)` 返回此实例(短路 _build_binding);
    `build_role_fallback` 不读此 override(语义「主端 fake / 备端 real」)。
    调用 `clear_test_llm()` 恢复生产行为。

    参数:
        llm 必须是 `BaseChatModel` 实例(与生产路径 `_build_binding` 返回类型
            一致),允许测试用 mock `__init__` 桩造的 ChatDeepSeek。
    """
    if not isinstance(role, Role):
        raise TypeError(
            f"set_test_llm 仅接受 Role 枚举,实得 {type(role).__name__}={role!r};"
            "旧字符串 provider key(\"deepseek\"/\"audit_deepseek\"/"
            "\"compression_deepseek\")已废,请用 Role.MAIN / "
            "Role.AUDIT / Role.COMPRESSION"
        )
    _test_llm_overrides[role] = llm


def clear_test_llm(role: Role | None = None) -> None:
    """清除测试 LLM override。

    仅接受 `Role` 枚举或 `None`;字符串输入立即抛 TypeError(与 set_test_llm 对称)。
    `role=None` 时清除全部;否则仅清除指定 role。
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
