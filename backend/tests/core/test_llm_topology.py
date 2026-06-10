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
)
from app.core.llm_topology import (
    ENDPOINTS,
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
        assert ep.base_url == "https://api.deepseek.com/v1"
        # 防尾随字符：长度等于标准长度
        assert len(ep.base_url) == len("https://api.deepseek.com/v1")
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

        _adapter_chat_deepseek("sk-ds-test", "https://api.deepseek.com/v1", ROLES[Role.MAIN])

        # 关注点 2 实证:不读实例属性,直接断言构造入参
        assert "temperature" not in captured, "main role 端不应传 temperature(走服务端默认)"
        assert captured["api_key"] == "sk-ds-test"
        assert captured["api_base"] == "https://api.deepseek.com/v1"
        assert captured["model"] == "deepseek-v4-flash"
        assert captured["timeout"] == LLM_REQUEST_TIMEOUT_SECONDS
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

        _adapter_chat_deepseek("sk-ds-test", "https://api.deepseek.com/v1", ROLES[Role.COMPRESSION])

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
        assert llm.api_base == "https://api.deepseek.com/v1"  # type: ignore[attr-defined]

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
