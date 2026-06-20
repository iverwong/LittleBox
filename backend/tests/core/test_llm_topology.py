"""Tests for app.core.llm_topology + Step 2 adapter 层（关注点 2/3 实证）。

Step 1 覆盖：
- resolve_profile 命中 / 未命中（最长前缀优先）
- ENDPOINTS 结构（base_url 纯字符串字面量 + api_key callable）
- ROLES 结构（main / audit / compression 三个 role 的字段）
- frozen dataclass 不可变性

Step 2 覆盖（adapter 层）:
- _adapter_chat_deepseek 构造入参 spy(关注点 2:不读实例属性,直接断言 kwargs)
- _TRANSPORTS dispatch 表(仅注册已实现 transport)
- _build_binding 装配链(关注点 3:不 monkeypatch ENDPOINTS,用 _FakeSettings 喂)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass
from typing import Any, get_type_hints

import pytest
from app.core.llm import (
    _TRANSPORTS,
    _adapter_chat_deepseek,
    _build_binding,
    _test_llm_overrides,
    build_compression_llm,
    build_crisis_llm,
    build_main_llm,
    build_role_fallback,
    build_role_primary,
    wrap_resilience,
)
from app.core.llm_topology import (
    ENDPOINTS,
    LLM_HTTPX_TIMEOUT,
    LLM_REQUEST_TIMEOUT_SECONDS,
    MODEL_PROFILES,
    ROLES,
    Endpoint,
    EndpointName,
    ModelProfile,
    ModelProfileNotRegisteredError,
    ReasoningEffort,
    Role,
    RoleBinding,
    Transport,
    resolve_profile,
)
from langchain_core.runnables import Runnable, RunnableWithFallbacks
from langchain_deepseek import ChatDeepSeek
from pydantic import SecretStr

# ============================================================================
# 1. resolve_profile 行为
# ============================================================================


class TestResolveProfileHits:
    """resolve_profile 命中场景：deepseek-v4 族前缀匹配 + family 自身命中。"""

    def test_dsv4_flash_hits_dsv4_family(self) -> None:
        """Given model="deepseek-v4-flash" When resolve_profile Then 返回 deepseek-v4 档。"""
        profile = resolve_profile("deepseek-v4-flash")
        assert profile.family == "deepseek-v4"
        assert profile.transport == Transport.CHAT_DEEPSEEK

    def test_dsv4_pro_hits_dsv4_family(self) -> None:
        """Given model="deepseek-v4-pro" When resolve_profile Then 返回 deepseek-v4 档。"""
        profile = resolve_profile("deepseek-v4-pro")
        assert profile.family == "deepseek-v4"

    def test_dsv4_family_itself_hits(self) -> None:
        """Given model="deepseek-v4"（family 自身作 model 名）When resolve_profile Then 命中。"""
        profile = resolve_profile("deepseek-v4")
        assert profile.family == "deepseek-v4"

    def test_supports_reasoning_true_for_dsv4(self) -> None:
        """deepseek-v4 档 supports_reasoning=True（影响 Step 5 extractor 解耦）。"""
        profile = resolve_profile("deepseek-v4-flash")
        assert profile.supports_reasoning is True
        assert profile.supports_tools is True
        assert profile.multimodal is False


class TestResolveProfileMiss:
    """resolve_profile 未命中：抛 ModelProfileNotRegisteredError。"""

    def test_unknown_model_raises(self) -> None:
        """Given model="qwen-vl" When resolve_profile Then 抛 ModelProfileNotRegisteredError。"""
        with pytest.raises(ModelProfileNotRegisteredError, match="qwen-vl"):
            resolve_profile("qwen-vl")

    def test_claude_raises(self) -> None:
        """Given model="claude-3-opus" When resolve_profile Then 抛错。"""
        with pytest.raises(ModelProfileNotRegisteredError, match="claude"):
            resolve_profile("claude-3-opus")

    def test_gpt_raises(self) -> None:
        """Given model="gpt-4o" When resolve_profile Then 抛错。"""
        with pytest.raises(ModelProfileNotRegisteredError, match="gpt-4o"):
            resolve_profile("gpt-4o")

    def test_empty_string_raises(self) -> None:
        """Given model="" When resolve_profile Then 抛错（不允许空字符串走「最长前缀=0」陷阱）。"""
        with pytest.raises(ModelProfileNotRegisteredError):
            resolve_profile("")


# ============================================================================
# 2. ENDPOINTS 结构
# ============================================================================


class TestEndpointsStructure:
    """ENDPOINTS 注册表结构：两条 base_url 必须是纯字符串字面量 + api_key callable。"""

    def test_endpoints_has_deepseek_and_bailian(self) -> None:
        """ENDPOINTS 含 DEEPSEEK + BAILIAN 两个 key（StrEnum 完整枚举值）。"""
        assert EndpointName.DEEPSEEK in ENDPOINTS
        assert EndpointName.BAILIAN in ENDPOINTS
        assert len(ENDPOINTS) == 2

    def test_deepseek_base_url_is_pure_string_literal(self) -> None:
        """DEEPSEEK base_url 必须是裸字符串（闸门 B 关注点 5）。"""
        ep = ENDPOINTS[EndpointName.DEEPSEEK]
        assert ep.base_url == "https://api.deepseek.com"
        # 防尾随字符：长度等于标准长度
        assert len(ep.base_url) == len("https://api.deepseek.com")
        # 防误粘的 markdown 链接语法
        assert "](http" not in ep.base_url
        assert " " not in ep.base_url

    def test_bailian_base_url_is_pure_string_literal(self) -> None:
        """BAILIAN base_url 必须是裸字符串（闸门 B 关注点 5）。"""
        ep = ENDPOINTS[EndpointName.BAILIAN]
        assert ep.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert len(ep.base_url) == len("https://dashscope.aliyuncs.com/compatible-mode/v1")
        assert "](http" not in ep.base_url
        assert " " not in ep.base_url

    def test_endpoint_api_key_is_callable(self) -> None:
        """两条 api_key 都必须是 callable（不直接持有 SecretStr）。"""
        assert callable(ENDPOINTS[EndpointName.DEEPSEEK].api_key)
        assert callable(ENDPOINTS[EndpointName.BAILIAN].api_key)

    def test_endpoint_name_matches_key(self) -> None:
        """Endpoint.name 与 dict key 一致（防御未来 dict key 与 field 失同步）。"""
        for key, ep in ENDPOINTS.items():
            assert ep.name == key


# ============================================================================
# 3. ROLES 结构（main / audit / compression）
# ============================================================================


class TestRolesMain:
    """ROLES[MAIN] 字段：deepseek 主 + bailian 兜底，thinking=true，effort=max。"""

    def test_main_endpoint_is_deepseek(self) -> None:
        assert ROLES[Role.MAIN].endpoint == EndpointName.DEEPSEEK

    def test_main_model_is_dsv4_flash(self) -> None:
        assert ROLES[Role.MAIN].model == "deepseek-v4-flash"

    def test_main_thinking_enabled(self) -> None:
        assert ROLES[Role.MAIN].thinking is True

    def test_main_effort_is_max(self) -> None:
        assert ROLES[Role.MAIN].reasoning_effort == ReasoningEffort.MAX

    def test_main_temperature_is_none(self) -> None:
        """main temperature=None 走服务端默认（与今日 settings 一致）。"""
        assert ROLES[Role.MAIN].temperature is None

    def test_main_retry_attempts_is_3(self) -> None:
        """main retry_attempts=3（与今日 default 一致）。"""
        assert ROLES[Role.MAIN].retry_attempts == 3

    def test_main_fallback_to_bailian(self) -> None:
        """main 真兜底：fallback.endpoint == BAILIAN（修今日假兜底 bug）。"""
        fb = ROLES[Role.MAIN].fallback
        assert fb is not None
        assert fb.endpoint == EndpointName.BAILIAN

    def test_main_fallback_model_is_dsv4_flash(self) -> None:
        fb = ROLES[Role.MAIN].fallback
        assert fb is not None
        assert fb.model == "deepseek-v4-flash"

    def test_main_fallback_thinking_enabled(self) -> None:
        """main 兜底端也带思考（与主端一致）。"""
        fb = ROLES[Role.MAIN].fallback
        assert fb is not None
        assert fb.thinking is True

    def test_main_fallback_effort_is_max(self) -> None:
        fb = ROLES[Role.MAIN].fallback
        assert fb is not None
        assert fb.reasoning_effort == ReasoningEffort.MAX


class TestRolesAudit:
    """ROLES[AUDIT] 字段：与 main 今日等价（main/audit 模型参数恰好一致）。"""

    def test_audit_endpoint_is_deepseek(self) -> None:
        assert ROLES[Role.AUDIT].endpoint == EndpointName.DEEPSEEK

    def test_audit_thinking_enabled(self) -> None:
        assert ROLES[Role.AUDIT].thinking is True

    def test_audit_effort_is_max(self) -> None:
        assert ROLES[Role.AUDIT].reasoning_effort == ReasoningEffort.MAX

    def test_audit_fallback_to_bailian(self) -> None:
        """audit 也走 bailian 真兜底（今日行为不变）。"""
        fb = ROLES[Role.AUDIT].fallback
        assert fb is not None
        assert fb.endpoint == EndpointName.BAILIAN


class TestRolesCompression:
    """ROLES[COMPRESSION] 字段：thinking=关、temperature=0.3、retry_attempts=1。"""

    def test_compression_endpoint_is_deepseek(self) -> None:
        assert ROLES[Role.COMPRESSION].endpoint == EndpointName.DEEPSEEK

    def test_compression_thinking_disabled(self) -> None:
        """compression 思考关闭（与今日 settings.compression_thinking_enabled=False 一致）。"""
        assert ROLES[Role.COMPRESSION].thinking is False

    def test_compression_effort_is_none(self) -> None:
        """compression 不走 reasoning，effort 必须为 None。"""
        assert ROLES[Role.COMPRESSION].reasoning_effort is None

    def test_compression_temperature_is_0_3(self) -> None:
        """compression temperature=0.3 保稳定（与今日独立 builder 行为一致）。"""
        assert ROLES[Role.COMPRESSION].temperature == 0.3

    def test_compression_retry_attempts_is_1(self) -> None:
        """compression retry_attempts=1（Iver 拍板：避免后台压缩在主端抖动时放大重试）。"""
        assert ROLES[Role.COMPRESSION].retry_attempts == 1

    def test_compression_fallback_to_bailian(self) -> None:
        """compression 兜底首次拥有 bailian（今日裸实例无 fallback，本步补上）。"""
        fb = ROLES[Role.COMPRESSION].fallback
        assert fb is not None
        assert fb.endpoint == EndpointName.BAILIAN

    def test_compression_fallback_temperature_is_0_3(self) -> None:
        fb = ROLES[Role.COMPRESSION].fallback
        assert fb is not None
        assert fb.temperature == 0.3

    def test_compression_fallback_retry_attempts_is_dead_field(self) -> None:
        """⚠️ 死字段验证：fallback 的 retry_attempts=3（默认）永不被消费（关注点 3）。

        此断言不是为了「保护这个值」，而是为 Step 2/3 实现者钉死一个事实：
        `_build_role_llm` 只读顶层 `ROLES[role].retry_attempts`，不会读
        `fallback.retry_attempts`。若未来要让兜底也有自己的重试预算，需
        改 `_build_role_llm` 显式读两遍。
        """
        fb = ROLES[Role.COMPRESSION].fallback
        assert fb is not None
        # 当前实现：默认 3，但**不会被消费**
        assert fb.retry_attempts == 3
        # 顶层是 1（这是真正生效的值）
        assert ROLES[Role.COMPRESSION].retry_attempts == 1


# ============================================================================
# 4. frozen dataclass 不可变 + 类型注解 / dataclass 元数据
# ============================================================================


class TestFrozenDataclass:
    """3 个 dataclass 都 frozen=True：实例化后不可修改字段。"""

    def test_endpoint_is_frozen(self) -> None:
        ep = Endpoint(EndpointName.DEEPSEEK, "https://x", lambda s: s)
        with pytest.raises(FrozenInstanceError):
            ep.base_url = "https://y"  # type: ignore[misc]

    def test_model_profile_is_frozen(self) -> None:
        p = ModelProfile("deepseek-v4", Transport.CHAT_DEEPSEEK, True, True, False)
        with pytest.raises(FrozenInstanceError):
            p.family = "qwen-vl"  # type: ignore[misc]

    def test_role_binding_is_frozen(self) -> None:
        rb = RoleBinding(
            EndpointName.DEEPSEEK,
            "deepseek-v4-flash",
            True,
            ReasoningEffort.MAX,
            None,
        )
        with pytest.raises(FrozenInstanceError):
            rb.model = "claude-3"  # type: ignore[misc]

    def test_role_binding_fallback_is_frozen(self) -> None:
        """fallback 实例也是 frozen，递归不可变。"""
        rb = RoleBinding(
            EndpointName.DEEPSEEK,
            "deepseek-v4-flash",
            True,
            ReasoningEffort.MAX,
            None,
            fallback=RoleBinding(
                EndpointName.BAILIAN,
                "deepseek-v4-flash",
                True,
                ReasoningEffort.MAX,
                None,
            ),
        )
        with pytest.raises(FrozenInstanceError):
            rb.fallback.model = "claude-3"  # type: ignore[misc]

    def test_role_binding_hashable(self) -> None:
        """frozen + 所有字段可哈希 → 实例可哈希。"""
        rb = RoleBinding(
            EndpointName.DEEPSEEK,
            "deepseek-v4-flash",
            True,
            ReasoningEffort.MAX,
            None,
        )
        # 不抛即通过
        hash(rb)

    def test_endpoint_hashable(self) -> None:
        ep = Endpoint(EndpointName.DEEPSEEK, "https://x", lambda s: s)
        hash(ep)


class TestDataclassMetadata:
    """dataclass 元数据：is_dataclass、fields 数量、字段名（不含类型解析）。"""

    def test_endpoint_is_dataclass(self) -> None:
        assert is_dataclass(Endpoint)

    def test_model_profile_is_dataclass(self) -> None:
        assert is_dataclass(ModelProfile)

    def test_role_binding_is_dataclass(self) -> None:
        assert is_dataclass(RoleBinding)

    def test_role_binding_has_seven_fields(self) -> None:
        """RoleBinding 字段数 = 7（endpoint / model / thinking / reasoning_effort /
        temperature / fallback / retry_attempts）。"""
        rb = ROLES[Role.MAIN]
        names = [f.name for f in fields(rb)]
        assert names == [
            "endpoint",
            "model",
            "thinking",
            "reasoning_effort",
            "temperature",
            "fallback",
            "retry_attempts",
        ]


class TestRuntimeTypeHints:
    """运行时类型解析边界验证（关注点 2 补救）。

    Endpoint 因含 `Callable[[Settings], SecretStr]` 注解 → get_type_hints 抛 NameError。
    RoleBinding 自引用字段（fallback: RoleBinding | None）→ globals 里有定义 → 解析成功。
    """

    def test_endpoint_get_type_hints_raises_nameerror(self) -> None:
        """get_type_hints(Endpoint) 抛 NameError（Settings 不在运行时 globals）。"""
        with pytest.raises(NameError, match="Settings"):
            get_type_hints(Endpoint)

    def test_role_binding_get_type_hints_succeeds(self) -> None:
        """get_type_hints(RoleBinding) 成功（自引用走 globals 解析，不触 Settings）。"""
        hints = get_type_hints(RoleBinding)
        # 字段全解析成功
        assert "endpoint" in hints
        assert "fallback" in hints
        assert "retry_attempts" in hints


# ============================================================================
# 5. 模块常量 + StrEnum 行为
# ============================================================================


class TestModuleConstants:
    """LLM_REQUEST_TIMEOUT_SECONDS 常量：60.0（与今日 settings 默认一致）。"""

    def test_timeout_constant(self) -> None:
        assert LLM_REQUEST_TIMEOUT_SECONDS == 60.0


class TestStrEnumValues:
    """StrEnum value 行为（关注点 C 子代理核实：value 是裸字符串）。"""

    def test_reasoning_effort_value_is_str(self) -> None:
        """StrEnum.value 返回裸字符串（Step 2 extra_body 序列化依赖此）。"""
        assert ReasoningEffort.MAX.value == "max"

    def test_role_value_is_str(self) -> None:
        assert Role.MAIN.value == "main"

    def test_endpoint_name_value_is_str(self) -> None:
        assert EndpointName.DEEPSEEK.value == "deepseek"

    def test_transport_value_is_str(self) -> None:
        assert Transport.CHAT_DEEPSEEK.value == "chat_deepseek"


class TestModelProfilesRegistry:
    """MODEL_PROFILES 元组结构：今日仅 deepseek-v4 一项。"""

    def test_has_dsv4_profile(self) -> None:
        families = [p.family for p in MODEL_PROFILES]
        assert "deepseek-v4" in families

    def test_dsv4_profile_flags(self) -> None:
        """deepseek-v4 档：transport / supports_reasoning / supports_tools / multimodal flag。"""
        p = next(p for p in MODEL_PROFILES if p.family == "deepseek-v4")
        assert p.transport == Transport.CHAT_DEEPSEEK
        assert p.supports_reasoning is True
        assert p.supports_tools is True
        assert p.multimodal is False


# ============================================================================
# 6. Step 2 · _FakeSettings + adapter 层（关注点 2/3 实证）
# ============================================================================


class _FakeSettings:
    """最小 settings stub:仅暴露 _build_binding 所需的 deepseek_api_key / bailian_api_key。

    关注点 3:不 monkeypatch 模块级 ENDPOINTS,而是 _FakeSettings 暴露真实
    SecretStr 字段;ENDPOINTS 的 api_key 是 `lambda s: s.deepseek_api_key` /
    `lambda s: s.bailian_api_key`,与 _FakeSettings 字段名天然兼容,被测面
    的真 ENDPOINTS 不动。
    """

    def __init__(
        self,
        *,
        deepseek_api_key: str = "sk-ds-test",
        bailian_api_key: str = "sk-bl-test",
    ) -> None:
        self.deepseek_api_key = SecretStr(deepseek_api_key)
        self.bailian_api_key = SecretStr(bailian_api_key)


class TestAdapterChatDeepseekKwargs:
    """_adapter_chat_deepseek 构造入参 spy 测试（关注点 2 实证）。

    关注点 2:不读实例属性(ChatDeepSeek 有默认 temperature,读 llm.temperature
    分不清「没传」与「传了默认」),改为 monkeypatch __init__ 截 kwargs,断言
    「main 端 kwargs 不含 temperature」+「compression 端 extra_body 不含
    reasoning_effort 键」是真断言。
    """

    def test_main_role_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Given ROLES[MAIN] When _adapter_chat_deepseek Then kwargs 含 6 字段、无 temperature。"""
        captured: dict[str, Any] = {}

        def mock_init(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(ChatDeepSeek, "__init__", mock_init)

        _adapter_chat_deepseek("sk-ds-test", "https://api.deepseek.com", ROLES[Role.MAIN])

        # 关注点 2 实证:不读实例属性,直接断言构造入参
        assert "temperature" not in captured, "main role 端不应传 temperature(走服务端默认)"
        assert captured["api_key"] == "sk-ds-test"
        assert captured["api_base"] == "https://api.deepseek.com"
        assert captured["model"] == "deepseek-v4-flash"
        # 关注点 6:timeout 共享 LLM_HTTPX_TIMEOUT(与 shared_http_client
        # 单一真相源,避免两边 httpx.Timeout 字面量漂移)
        assert captured["timeout"] is LLM_HTTPX_TIMEOUT
        assert captured["timeout"].read == LLM_REQUEST_TIMEOUT_SECONDS
        assert captured["max_retries"] == 0
        assert captured["extra_body"] == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        }

    def test_compression_role_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Given ROLES[COMPRESSION] When adapter Then kwargs 含 0.3 温度, extra_body 无 effort。"""
        captured: dict[str, Any] = {}

        def mock_init(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(ChatDeepSeek, "__init__", mock_init)

        _adapter_chat_deepseek("sk-ds-test", "https://api.deepseek.com", ROLES[Role.COMPRESSION])

        assert captured["temperature"] == 0.3
        # 关注点 2 实证:extra_body 真不含 reasoning_effort(不是读了默认 dict 的伪存在)
        assert "reasoning_effort" not in captured["extra_body"]
        assert captured["extra_body"]["thinking"] == {"type": "disabled"}
        assert captured["model"] == "deepseek-v4-flash"

    def test_reasoning_effort_value_is_str_literally(self) -> None:
        """Given ROLES[MAIN].reasoning_effort=MAX When extra_body 序列化 Then 是裸字符串 'max'。"""
        b = ROLES[Role.MAIN]
        # 关注点 C 子代理核实:StrEnum.value 是裸字符串(不走 str(e) 隐式路径)
        assert b.reasoning_effort is not None
        assert b.reasoning_effort.value == "max"
        assert isinstance(b.reasoning_effort.value, str)


class TestTransportsTable:
    """_TRANSPORTS dispatch 表:键 = Transport 枚举,只注册已实现的。"""

    def test_only_chat_deepseek_registered(self) -> None:
        """_TRANSPORTS 仅含 CHAT_DEEPSEEK 一个键(未实现 transport 不注册)。"""
        assert Transport.CHAT_DEEPSEEK in _TRANSPORTS
        assert len(_TRANSPORTS) == 1

    def test_chat_deepseek_value_is_adapter(self) -> None:
        """_TRANSPORTS[CHAT_DEEPSEEK] 指向 _adapter_chat_deepseek。"""
        assert _TRANSPORTS[Transport.CHAT_DEEPSEEK] is _adapter_chat_deepseek


class TestBuildBinding:
    """_build_binding 装配层(关注点 3:不 monkeypatch ENDPOINTS,真拓扑留被测面)。"""

    def test_main_returns_chat_deepseek(self) -> None:
        """Given ROLES[MAIN] + _FakeSettings When _build_binding Then 返回 ChatDeepSeek 实例。"""
        s = _FakeSettings()
        llm = _build_binding(ROLES[Role.MAIN], s)
        assert isinstance(llm, ChatDeepSeek)

    def test_compression_returns_chat_deepseek(self) -> None:
        """Given ROLES[COMPRESSION] When _build_binding Then 返回 ChatDeepSeek 实例。"""
        s = _FakeSettings()
        llm = _build_binding(ROLES[Role.COMPRESSION], s)
        assert isinstance(llm, ChatDeepSeek)

    def test_main_uses_deepseek_endpoint(self) -> None:
        """_build_binding(ROLES[MAIN]) 走 deepseek 端点(api_base 字段)。"""
        s = _FakeSettings()
        llm = _build_binding(ROLES[Role.MAIN], s)
        # ChatDeepSeek.api_base 字段(继承自 BaseChatOpenAI)
        assert llm.api_base == "https://api.deepseek.com"  # type: ignore[attr-defined]

    def test_main_fallback_uses_bailian_endpoint(self) -> None:
        """_build_binding(ROLES[MAIN].fallback) 走 bailian 端点(真兜底路径)。"""
        s = _FakeSettings()
        fb = ROLES[Role.MAIN].fallback
        assert fb is not None
        llm = _build_binding(fb, s)
        assert llm.api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"  # type: ignore[attr-defined]

    def test_audit_fallback_uses_bailian_endpoint(self) -> None:
        """_build_binding(ROLES[AUDIT].fallback) 走 bailian 端点(今日 audit 兜底行为)。"""
        s = _FakeSettings()
        fb = ROLES[Role.AUDIT].fallback
        assert fb is not None
        llm = _build_binding(fb, s)
        assert llm.api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"  # type: ignore[attr-defined]

    def test_unknown_model_raises(self) -> None:
        """_build_binding 对未注册 model 名抛 ModelProfileNotRegisteredError。"""
        s = _FakeSettings()
        bad_binding = RoleBinding(
            endpoint=EndpointName.DEEPSEEK,
            model="qwen-vl",
            thinking=True,
            reasoning_effort=ReasoningEffort.MAX,
            temperature=None,
        )
        with pytest.raises(ModelProfileNotRegisteredError, match="qwen-vl"):
            _build_binding(bad_binding, s)


# ============================================================================
# 7. Step 3 · role 驱动入口 + 注入缝（关注点 2/3/4 实证）
# ============================================================================
# 覆盖:
# - build_role_primary / build_role_fallback：直接返回裸 ChatModel 实例（未包
#   retry / fallback）—— 这是 audit/llm.py 后续 .bind_tools() 的前提
# - 注入缝：set_test_llm(Role.MAIN, fake) 短路 build_role_primary；
#   build_role_fallback 不读 override（语义「主端 fake / 备端 real」不变）
# - wrap_resilience 链式形态：primary.with_retry().with_fallbacks([fallback])
# - _build_role_llm / build_main_llm / build_crisis_llm /
#   build_compression_llm 串联三者,retry 次数取 ROLES[role].retry_attempts


class _FakeRunnable:
    """最小 Runnable 假实现：仅暴露 wrap_resilience 链路所需的 with_retry/with_fallbacks。

    用于注入缝测试：build_role_primary 短路返回此类。
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def with_retry(self, **kwargs: Any) -> Runnable:
        self.calls.append(("with_retry", kwargs))
        return self

    def with_fallbacks(self, fallbacks: list, **kwargs: Any) -> Runnable:
        self.calls.append(("with_fallbacks", fallbacks, kwargs))
        return self

    def bind_tools(self, tools: list, **kwargs: Any) -> Runnable:
        self.calls.append(("bind_tools", tools, kwargs))
        return self

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        return self

    async def astream(self, *args: Any, **kwargs: Any):  # noqa: A003
        yield self


class TestBuildRolePrimary:
    """build_role_primary：返回裸 ChatModel 实例（未包 retry / fallback）。"""

    def test_returns_chat_deepseek(self) -> None:
        """Given ROLES[MAIN] When build_role_primary(MAIN) Then 返回 ChatDeepSeek 裸实例。"""
        s = _FakeSettings()
        llm = build_role_primary(Role.MAIN, s)
        assert isinstance(llm, ChatDeepSeek)
        # 关注点 3 实证:不是 RunnableWithFallbacks（裸实例，audit/llm.py 可直接 .bind_tools()）
        assert not isinstance(llm, RunnableWithFallbacks)

    def test_returns_chat_deepseek_for_compression(self) -> None:
        """build_role_primary(COMPRESSION) 返回 ChatDeepSeek 裸实例（temperature 走 adapter）。"""
        s = _FakeSettings()
        llm = build_role_primary(Role.COMPRESSION, s)
        assert isinstance(llm, ChatDeepSeek)
        # 关注点 3 实证：compression 主端也是裸实例
        assert not isinstance(llm, RunnableWithFallbacks)


class TestBuildRoleFallback:
    """build_role_fallback：返回 ROLES[role].fallback 的裸实例（不查 override）。"""

    def test_main_fallback_uses_bailian(self) -> None:
        """build_role_fallback(MAIN) 走 bailian 端点（真兜底）。"""
        s = _FakeSettings()
        fb = build_role_fallback(Role.MAIN, s)
        assert fb is not None
        assert isinstance(fb, ChatDeepSeek)
        assert fb.api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"  # type: ignore[attr-defined]

    def test_compression_fallback_uses_bailian(self) -> None:
        """build_role_fallback(COMPRESSION) 走 bailian 端点。"""
        s = _FakeSettings()
        fb = build_role_fallback(Role.COMPRESSION, s)
        assert fb is not None
        assert fb.api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"  # type: ignore[attr-defined]

    def test_fallback_does_not_check_override(self, llm_override: Any) -> None:
        """关注点 2 实证：build_role_fallback 不读 _test_llm_overrides（主端 fake / 备端 real）。"""
        s = _FakeSettings()
        fake = _FakeRunnable()
        llm_override(Role.AUDIT, fake)
        # 即使 AUDIT role 被 override,build_role_fallback 仍走真 bailian
        fb = build_role_fallback(Role.AUDIT, s)
        assert fb is not None
        assert fb is not fake  # 不是 override
        assert isinstance(fb, ChatDeepSeek)


class TestWrapResilience:
    """wrap_resilience：primary.with_retry(...).with_fallbacks([fallback]) 链式形态。"""

    def test_chains_retry_then_fallback(self) -> None:
        """Given primary + fallback When wrap_resilience Then with_retry + with_fallbacks 调用。"""
        primary = _FakeRunnable()
        fallback = _FakeRunnable()
        wrap_resilience(primary, fallback, retry_attempts=3)
        # _FakeRunnable 链式返回 self,关注点 1 实证:链形态正确
        assert "with_retry" in [c[0] for c in primary.calls]
        assert "with_fallbacks" in [c[0] for c in primary.calls]
        # with_fallbacks 接收的 fallbacks 列表含 fallback 实例
        wf_call = next(c for c in primary.calls if c[0] == "with_fallbacks")
        assert wf_call[1] == [fallback]

    def test_retry_attempts_1_no_fallback_chain(self) -> None:
        """retry_attempts=1 仍走 with_fallbacks 链（'1 = 不重试,仍走 fallback',plan 注释）。"""
        primary = _FakeRunnable()
        fallback = _FakeRunnable()
        wrap_resilience(primary, fallback, retry_attempts=1)
        assert "with_fallbacks" in [c[0] for c in primary.calls]

    def test_no_fallback_returns_retryable_only(self) -> None:
        """fallback is None → 仅返回 retryable primary,不调 with_fallbacks。"""
        primary = _FakeRunnable()
        wrap_resilience(primary, None, retry_attempts=3)
        assert "with_retry" in [c[0] for c in primary.calls]
        assert "with_fallbacks" not in [c[0] for c in primary.calls]


class TestBuildRoleLlm:
    """_build_role_llm：primary + retry + fallback 一体化,retry 取 ROLES[role].retry_attempts。"""

    def test_returns_runnable_with_fallbacks(self) -> None:
        """_build_role_llm(MAIN) 返回 RunnableWithFallbacks（含 retry + 1 fallback）。"""

        s = _FakeSettings()
        result = _build_role_llm_for_test(Role.MAIN, s)  # 见下 helper
        # 包装后是 RunnableWithFallbacks（含 1 个 fallback = bailian）
        assert isinstance(result, RunnableWithFallbacks)
        assert len(result.fallbacks) == 1  # type: ignore[attr-defined]

    def test_compression_retry_attempts_1(self) -> None:
        """compression role 的 retry_attempts=1（plan #2 Iver 拍板）。"""
        assert ROLES[Role.COMPRESSION].retry_attempts == 1

    def test_main_retry_attempts_3(self) -> None:
        """main role 的 retry_attempts=3（与旧 build_main_llm 一致）。"""
        assert ROLES[Role.MAIN].retry_attempts == 3

    def test_audit_retry_attempts_3(self) -> None:
        """audit role 的 retry_attempts=3（与旧 build_audit_llm 一致）。"""
        assert ROLES[Role.AUDIT].retry_attempts == 3


def _build_role_llm_for_test(role, settings):
    """测试 helper:从 llm 顶层拿 _build_role_llm(避免循环 import)。"""
    from app.core.llm import _build_role_llm

    return _build_role_llm(role, settings)


class TestBuildMainLlm:
    """build_main_llm：role=MAIN 入口（crisis 也走此）。"""

    def test_returns_runnable_with_fallbacks(self) -> None:
        """build_main_llm 返回 RunnableWithFallbacks（retry=3 + bailian 兜底）。"""
        s = _FakeSettings()
        result = build_main_llm(s)
        assert isinstance(result, RunnableWithFallbacks)
        assert len(result.fallbacks) == 1  # type: ignore[attr-defined]


class TestBuildCrisisLlm:
    """build_crisis_llm：role=MAIN 复用（关注点 #6 crisis 重锦到 main）。"""

    def test_uses_main_binding(self) -> None:
        """build_crisis_llm 与 build_main_llm 行为一致（同 Role.MAIN 绑定）。"""
        s = _FakeSettings()
        result = build_crisis_llm(s)
        assert isinstance(result, RunnableWithFallbacks)
        # 关注点 #6 实证:crisis 与 main 走同一 fallback 路径
        assert len(result.fallbacks) == 1  # type: ignore[attr-defined]


class TestBuildCompressionLlm:
    """build_compression_llm：role=COMPRESSION 入口（pipeline.py 切换点）。"""

    def test_returns_runnable_with_fallbacks(self) -> None:
        """build_compression_llm 返回 RunnableWithFallbacks（retry=1 + bailian 兜底）。"""
        s = _FakeSettings()
        result = build_compression_llm(s)
        assert isinstance(result, RunnableWithFallbacks)
        assert len(result.fallbacks) == 1  # type: ignore[attr-defined]


class TestInjectionSeamRoleKey:
    """注入缝:_test_llm_overrides 键为 Role 枚举;set/clear 仅接受 Role。"""

    def test_set_role_enum_short_circuits_primary(self, llm_override: Any) -> None:
        """set_test_llm(Role.MAIN, fake) → build_role_primary(MAIN) 返回 fake。"""
        s = _FakeSettings()
        fake = _FakeRunnable()
        llm_override(Role.MAIN, fake)
        result = build_role_primary(Role.MAIN, s)
        assert result is fake

    def test_set_role_string_raises_type_error(self) -> None:
        """set_test_llm("字符串", fake) 抛 TypeError(运行时不依赖注解,isinstance 显式守卫)。

        关注点 1:Python 注解运行时不强制,且 tests/ 不在 basedpyright 范围——
        漏改字符串调用不会自动报错,而是 _test_llm_overrides["字符串"] 静默不命中
        (override 失效、回落真 LLM,难诊断)。显式 isinstance 守卫在 setup 阶段立即
        抛出,任何漏改立即可见。
        """
        from app.core.llm import set_test_llm

        with pytest.raises(TypeError, match="仅接受 Role"):
            set_test_llm("deepseek", _FakeRunnable())  # type: ignore[arg-type]

    def test_clear_role_string_raises_type_error(self) -> None:
        """clear_test_llm("字符串") 抛 TypeError(对称守卫)。"""
        from app.core.llm import clear_test_llm

        with pytest.raises(TypeError, match="仅接受 Role"):
            clear_test_llm("audit_deepseek")  # type: ignore[arg-type]

    def test_clear_role_enum_removes_only_that_role(self, llm_override: Any) -> None:
        """clear_test_llm(Role.MAIN) 只清 Role.MAIN,Role.AUDIT 仍 override。"""
        from app.core.llm import clear_test_llm

        s = _FakeSettings()
        fake_main = _FakeRunnable()
        fake_audit = _FakeRunnable()
        llm_override(Role.MAIN, fake_main)
        llm_override(Role.AUDIT, fake_audit)
        clear_test_llm(Role.MAIN)
        # MAIN 恢复真实装配
        assert build_role_primary(Role.MAIN, s) is not fake_main
        # AUDIT 仍 override
        assert build_role_primary(Role.AUDIT, s) is fake_audit

    def test_clear_all_clears_everything(self, llm_override: Any) -> None:
        """clear_test_llm() 不带参 → 清空全部。"""
        from app.core.llm import clear_test_llm

        llm_override(Role.MAIN, _FakeRunnable())
        llm_override(Role.AUDIT, _FakeRunnable())
        clear_test_llm()
        assert _test_llm_overrides == {}

    def test_overrides_dict_uses_role_keys(self, llm_override: Any) -> None:
        """_test_llm_overrides 键为 Role 枚举,非字符串(关注点 2 实证)。"""
        llm_override(Role.MAIN, _FakeRunnable())
        llm_override(Role.AUDIT, _FakeRunnable())
        assert Role.MAIN in _test_llm_overrides
        assert Role.AUDIT in _test_llm_overrides
        # 不应有字符串 key
        assert "deepseek" not in _test_llm_overrides
        assert "audit_deepseek" not in _test_llm_overrides
