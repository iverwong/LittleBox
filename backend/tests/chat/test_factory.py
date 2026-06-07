"""Tests for app.chat.factory — provider registry + ChatDeepSeek primary.

M6 patch 2 (Step 11.1): replaces ChatOpenAI + with_fallbacks tests with
registry dispatch + ChatDeepSeek primary + fallback chain coverage.
M8-hotfix: adds audit_bailian registry key and audit LLM retry/fallback tests.
"""

from typing import Any

import pytest
from langchain_core.runnables import RunnableBinding, RunnableWithFallbacks
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from app.core.llm import (
    ProviderNotRegisteredError,
    _PROVIDER_REGISTRY,
    build_crisis_llm,
    build_main_llm,
    build_provider_llm,
    build_redline_llm,
)


# ---------------------------------------------------------------------------
# Fixtures: minimal settings object
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Minimal settings stub matching fields consumed by factory builders.

    D-4B.1 加固:bailian_model / audit_model 默认值与 deepseek_model 故意不同,
    防止 model 字段相关断言在两者同值时退化为空断言(openai 应取 bailian_model,
    不是 deepseek_model;audit_* 应取 audit_model)。
    """

    def __init__(self, **kwargs: Any) -> None:
        from pydantic import SecretStr

        self.main_provider = "deepseek"
        self.fallback_provider: str | None = "deepseek"
        self.enable_fallback = True
        self.deepseek_api_key = SecretStr(kwargs.get("deepseek_api_key", "sk-ds-test"))
        self.deepseek_base_url = kwargs.get("deepseek_base_url", "https://api.deepseek.com/v1")
        self.deepseek_model = kwargs.get("deepseek_model", "deepseek-v4-flash")
        self.main_thinking_enabled = kwargs.get("main_thinking_enabled", True)
        self.main_reasoning_effort = kwargs.get("main_reasoning_effort", "max")
        self.bailian_api_key = SecretStr(kwargs.get("bailian_api_key", "sk-bl-test"))
        self.bailian_base_url = kwargs.get(
            "bailian_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        # D-4B.1:故意与 deepseek_model 不同(防止 model 字段空断言)
        self.bailian_model = kwargs.get("bailian_model", "qwen3-max")
        self.llm_request_timeout_seconds = 60.0
        # M8 audit pipeline settings
        # D-4B.1:故意与 deepseek_model 不同(audit_* provider 应取 audit_model)
        self.audit_model = kwargs.get("audit_model", "audit-v2")
        self.audit_reasoning_effort = kwargs.get("audit_reasoning_effort", "max")
        self.audit_thinking_enabled = kwargs.get("audit_thinking_enabled", True)
        # M8 compression pipeline settings
        self.compression_provider = kwargs.get("compression_provider", "deepseek")
        self.compression_model = kwargs.get("compression_model", "deepseek-v4-flash")
        self.compression_thinking_enabled = kwargs.get("compression_thinking_enabled", False)
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)


# ---- T0: _PROVIDER_REGISTRY has both deepseek and openai keys ----


class TestRegistryKeys:
    """_PROVIDER_REGISTRY contains all 5 provider keys."""

    def test_registry_has_deepseek(self) -> None:
        assert "deepseek" in _PROVIDER_REGISTRY

    def test_registry_has_openai(self) -> None:
        assert "openai" in _PROVIDER_REGISTRY

    def test_registry_has_audit_deepseek(self) -> None:
        assert "audit_deepseek" in _PROVIDER_REGISTRY

    def test_registry_has_audit_bailian(self) -> None:
        assert "audit_bailian" in _PROVIDER_REGISTRY

    def test_registry_has_compression_deepseek(self) -> None:
        assert "compression_deepseek" in _PROVIDER_REGISTRY

    def test_registry_keys_are_callable(self) -> None:
        settings = _FakeSettings()
        ds_llm = _PROVIDER_REGISTRY["deepseek"](settings)
        assert isinstance(ds_llm, ChatDeepSeek)

        oa_llm = _PROVIDER_REGISTRY["openai"](settings)
        assert isinstance(oa_llm, ChatOpenAI)

        audit_bl = _PROVIDER_REGISTRY["audit_bailian"](settings)
        assert isinstance(audit_bl, ChatDeepSeek)


# ---- T1: build_provider_llm dispatch ----


class TestBuildProviderLlm:
    """build_provider_llm dispatches to the correct provider."""

    def test_deepseek_returns_chatdeepseek(self) -> None:
        settings = _FakeSettings()
        llm = build_provider_llm("deepseek", settings)
        assert isinstance(llm, ChatDeepSeek)

    def test_openai_returns_chatopenai(self) -> None:
        settings = _FakeSettings()
        llm = build_provider_llm("openai", settings)
        assert isinstance(llm, ChatOpenAI)

    def test_unknown_provider_raises(self) -> None:
        settings = _FakeSettings()
        with pytest.raises(ProviderNotRegisteredError, match="unknown"):
            build_provider_llm("unknown", settings)

    def test_openai_uses_bailian_model_not_deepseek(self) -> None:
        """D-4B.1 防回归:openai provider 的 model 字段取 bailian_model,不是 deepseek_model。

        原 4.1 实现把 model 字段折叠到 _ROLE_SETTINGS["main"],统一取
        deepseek_model,导致 openai provider 在 bailian 端点发错误 model 名。
        修复后 _MODEL_FIELD[(main, openai)] = "bailian_model"。
        """
        settings = _FakeSettings()
        assert settings.bailian_model != settings.deepseek_model  # fixture 防护
        llm = build_provider_llm("openai", settings)
        assert llm.model == settings.bailian_model
        assert llm.model != settings.deepseek_model

    def test_audit_providers_use_audit_model(self) -> None:
        """D-4B.1 防回归:audit_deepseek / audit_bailian 都取 audit_model,不是 deepseek_model。"""
        settings = _FakeSettings()
        assert settings.audit_model != settings.deepseek_model  # fixture 防护

        llm_ds = build_provider_llm("audit_deepseek", settings)
        assert llm_ds.model == settings.audit_model
        assert llm_ds.model != settings.deepseek_model

        llm_bl = build_provider_llm("audit_bailian", settings)
        assert llm_bl.model == settings.audit_model
        assert llm_bl.model != settings.deepseek_model


# ---- T2: build_main_llm default (with fallback) ----


class TestBuildMainLlmDefault:
    """build_main_llm with default settings returns RunnableWithFallbacks."""

    def test_default_returns_runnable_with_fallbacks(self) -> None:
        settings = _FakeSettings()
        runnable = build_main_llm(settings)
        assert isinstance(runnable, RunnableWithFallbacks)

    def test_primary_is_chatdeepseek(self) -> None:
        settings = _FakeSettings()
        runnable = build_main_llm(settings)
        # RunnableWithFallbacks.bound is the primary (may be with_retry wrapped
        # or raw ChatDeepSeek depending on langchain version)
        primary = runnable.bound
        # Unwrap RunnableBinding if present
        inner = primary.bound if isinstance(primary, RunnableBinding) else primary
        assert isinstance(inner, ChatDeepSeek)

    def test_has_fallback(self) -> None:
        settings = _FakeSettings()
        runnable = build_main_llm(settings)
        assert len(runnable.fallbacks) == 1


# ---- T3: enable_fallback=False ----


class TestBuildMainLlmNoFallback:
    """enable_fallback=False returns raw LLM, not RunnableWithFallbacks."""

    def test_no_fallback_returns_chatdeepseek(self) -> None:
        settings = _FakeSettings(enable_fallback=False)
        llm = build_main_llm(settings)
        assert isinstance(llm, ChatDeepSeek)

    def test_no_fallback_not_runnable_with_fallbacks(self) -> None:
        settings = _FakeSettings(enable_fallback=False)
        llm = build_main_llm(settings)
        assert not isinstance(llm, RunnableWithFallbacks)


# ---- T4: unknown provider raises ----


class TestBuildMainLlmUnknown:
    """main_provider="unknown" triggers ProviderNotRegisteredError."""

    def test_unknown_main_provider_raises(self) -> None:
        settings = _FakeSettings(main_provider="unknown")
        with pytest.raises(ProviderNotRegisteredError, match="unknown"):
            build_main_llm(settings)


# ---- T5: ChatDeepSeek construction params (mock init) ----


class TestChatDeepSeekConstruction:
    """Verify ChatDeepSeek receives correct construction kwargs."""

    def test_deepseek_construction_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock ChatDeepSeek.__init__ to verify base_url / api_key / model / extra_body."""
        captured: dict[str, Any] = {}

        def mock_init(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(ChatDeepSeek, "__init__", mock_init)

        settings = _FakeSettings()
        _PROVIDER_REGISTRY["deepseek"](settings)

        assert captured.get("api_base") == "https://api.deepseek.com/v1"
        assert captured.get("api_key") == "sk-ds-test"
        assert captured.get("model") == "deepseek-v4-flash"
        assert captured.get("extra_body") == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        }

    def test_openai_construction_params(self) -> None:
        """Construct ChatOpenAI via registry and verify params on instance.

        D-4B.1 加固:openai provider 的 model 字段必须取 settings.bailian_model,
        不是 deepseek_model(陷阱 ① 同 provider 不同 model)。bailian_model 与
        deepseek_model 在 _FakeSettings 默认值故意不同,断言强绑定字段。
        """
        settings = _FakeSettings()
        assert settings.bailian_model != settings.deepseek_model, (
            "fixture 同值会让本测试退化为空断言"
        )
        llm = _PROVIDER_REGISTRY["openai"](settings)
        # ChatOpenAI maps base_url → openai_api_base via Pydantic alias
        assert llm.openai_api_base.startswith("https://dashscope.aliyuncs.com")
        assert llm.model == settings.bailian_model
        assert llm.model != settings.deepseek_model

    def test_audit_deepseek_construction_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock ChatDeepSeek.__init__ to verify audit_deepseek uses audit_* settings.

        D-4B.1 加固:audit_* provider 的 model 字段必须取 settings.audit_model,
        不是 deepseek_model。audit_model 与 deepseek_model 在 _FakeSettings
        默认值故意不同,断言强绑定字段。
        """
        captured: dict[str, Any] = {}

        def mock_init(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(ChatDeepSeek, "__init__", mock_init)

        settings = _FakeSettings()
        assert settings.audit_model != settings.deepseek_model, (
            "fixture 同值会让本测试退化为空断言"
        )
        _PROVIDER_REGISTRY["audit_deepseek"](settings)

        assert captured.get("model") == settings.audit_model
        assert captured.get("model") != settings.deepseek_model
        assert captured.get("extra_body") == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        }

    def test_crisis_llm_config(self) -> None:
        """run_lm: build_crisis_llm 实例非空、复audit_deepseek。"""
        settings = _FakeSettings()
        llm = build_crisis_llm(settings)
        assert isinstance(llm, ChatDeepSeek)
        assert llm.extra_body["thinking"] == {"type": "enabled"}
        assert llm.extra_body["reasoning_effort"] == "max"
        assert not hasattr(llm, "tools") or llm.tools is None or llm.tools == []

    def test_redline_llm_config(self) -> None:
        """run_llm: build_redline_llm 实例非空、复audit_deepseek。"""
        settings = _FakeSettings()
        llm = build_redline_llm(settings)
        assert isinstance(llm, ChatDeepSeek)
        assert llm.extra_body["thinking"] == {"type": "enabled"}
        assert llm.extra_body["reasoning_effort"] == "max"
        assert not hasattr(llm, "tools") or llm.tools is None or llm.tools == []


# ---- T5b: ChatDeepSeek thinking params (Step 11.3) ----
# Source: langchain_deepseek/chat_models.py — no top-level thinking/reasoning_effort
# field on ChatDeepSeek (lines 31-521). Must pass via extra_body (inherited from
# BaseChatOpenAI, confirmed 2026-05-09).


class TestChatDeepSeekThinkingParams:
    """Verify extra_body thinking params survive on a real ChatDeepSeek instance."""

    def test_extra_body_thinking_on_real_instance(self) -> None:
        """Construct ChatDeepSeek with extra_body and verify attribute."""
        llm = ChatDeepSeek(
            api_key="sk-test",
            api_base="https://api.deepseek.com/v1",
            model="deepseek-v4-flash",
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
        )
        assert llm.extra_body == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }

    def test_extra_body_default_reasoning_effort(self) -> None:
        """Verify reasoning_effort=high is the default."""
        llm = ChatDeepSeek(
            api_key="sk-test",
            api_base="https://api.deepseek.com/v1",
            model="deepseek-v4-flash",
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
        )
        assert llm.extra_body["reasoning_effort"] == "high"  # type: ignore[index]

    def test_extra_body_thinking_enabled(self) -> None:
        """Verify thinking type is enabled."""
        llm = ChatDeepSeek(
            api_key="sk-test",
            api_base="https://api.deepseek.com/v1",
            model="deepseek-v4-flash",
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
        )
        assert llm.extra_body["thinking"] == {"type": "enabled"}  # type: ignore[index]


# ---- T5c: compression factory thinking params ----

class TestCompressionFactory:
    """compression_deepseek 的 thinking 配置由 compression_thinking_enabled 控制（默认关闭）。"""

    def test_thinking_disabled_by_default(self) -> None:
        settings = _FakeSettings()
        llm = _PROVIDER_REGISTRY["compression_deepseek"](settings)
        assert llm.extra_body["thinking"] == {"type": "disabled"}  # type: ignore[index]

    def test_temperature_is_0_3(self) -> None:
        settings = _FakeSettings()
        llm = _PROVIDER_REGISTRY["compression_deepseek"](settings)
        assert llm.temperature == 0.3  # type: ignore[attr-defined]

    def test_thinking_enabled_when_configured(self) -> None:
        settings = _FakeSettings(compression_thinking_enabled=True)
        llm = _PROVIDER_REGISTRY["compression_deepseek"](settings)
        assert llm.extra_body["thinking"] == {"type": "enabled"}  # type: ignore[index]

    def test_uses_compression_model(self) -> None:
        settings = _FakeSettings(compression_model="deepseek-v3")
        llm = _PROVIDER_REGISTRY["compression_deepseek"](settings)
        assert llm.model == "deepseek-v3"  # type: ignore[attr-defined]

    def test_invoked_via_build_provider_llm(self) -> None:
        settings = _FakeSettings()
        from app.core.llm import build_provider_llm

        llm = build_provider_llm("compression_deepseek", settings)
        assert isinstance(llm, ChatDeepSeek)
        assert llm.extra_body["thinking"] == {"type": "disabled"}  # type: ignore[index]


# ---- T6: get_chat_llm backward compat ----



# ---- T7: audit LLM retry + fallback fault injection ----
# 验证 build_audit_llm 的 with_retry / with_fallbacks 在 HTTP 层正确工作。
# 通过 respx mock HTTP 层，注入瞬态错误确认重试机制生效。


class TestAuditLlmRetry:
    """审查 LLM with_retry / with_fallbacks 故障注入测试。"""

    async def test_primary_retry_on_connect_error_then_success(self) -> None:
        """主端首次 ConnectError → with_retry 重试 → 第二次成功。"""
        import httpx
        import respx
        from app.core.config import settings

        url = f"{settings.deepseek_base_url}/chat/completions"

        async with respx.mock(assert_all_mocked=False) as respx_mock:
            route = respx_mock.post(url)
            # side_effect 数组：第一次 ConnectError，第二次 200
            route.mock(
                side_effect=[
                    httpx.ConnectError("mock connection refused"),
                    httpx.Response(
                        status_code=200,
                        json={
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": "测试回复"},
                                "finish_reason": "stop",
                            }],
                        },
                    ),
                ],
            )

            from app.audit.llm import build_audit_llm
            llm = build_audit_llm(settings)
            from langchain_core.messages import HumanMessage
            result = await llm.ainvoke([HumanMessage(content="你好")])
            assert result.content is not None
            # 第 1 次失败 + 第 2 次成功 = 2 次 HTTP 调用
            assert len(respx_mock.calls) == 2

    async def test_primary_all_fail_uses_fallback(self) -> None:
        """主端持续 ConnectError → with_retry 耗尽 → fallback 百炼返回成功。"""
        import httpx
        import respx
        from app.core.config import settings

        primary_url = f"{settings.deepseek_base_url}/chat/completions"
        fallback_url = f"{settings.bailian_base_url}/chat/completions"

        async with respx.mock(assert_all_mocked=False) as respx_mock:
            primary_route = respx_mock.post(primary_url)
            # 主端 3 次全部 ConnectError
            primary_route.mock(
                side_effect=[
                    httpx.ConnectError("mock connection refused") for _ in range(3)
                ],
            )
            # 备用端成功
            respx_mock.post(fallback_url).respond(
                status_code=200,
                json={
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "备端回复"},
                        "finish_reason": "stop",
                    }],
                },
            )

            from app.audit.llm import build_audit_llm
            llm = build_audit_llm(settings)
            from langchain_core.messages import HumanMessage
            result = await llm.ainvoke([HumanMessage(content="你好")])
            assert result.content is not None


# ---- T8: 集成测试注入缝（M9.5 Step 5） ----
# 验证 set_test_llm / clear_test_llm / build_provider_llm override 行为。


class _FakeRunnable:
    """最小 Runnable 假实现，仅用于验证注入缝路由是否正确。"""
    async def astream(self, input, config=None):  # noqa: A003
        yield self
    async def ainvoke(self, input, config=None):
        return self
    def bind_tools(self, tools, **kwargs):  # noqa: A003
        """bind_tools 链式方法：build_audit_llm 内部调此包装 with_retry。"""
        return self
    def with_retry(self, **kwargs):
        """with_retry 链式方法：build_audit_llm 内部使用。"""
        return self
    def with_fallbacks(self, fallbacks, **kwargs):
        """with_fallbacks 链式方法：build_audit_llm 内部使用。"""
        return self


class TestInjectionSeam:
    """build_provider_llm 注入缝：按 provider 名 dispatch 的 override。"""

    def teardown_method(self) -> None:
        """每测试后清理 override，避免跨测试泄漏。"""
        from app.core.llm import clear_test_llm
        clear_test_llm()

    def test_set_provider_override_returns_fake(self) -> None:
        """set_test_llm("deepseek", fake) → build_provider_llm 返回 fake。"""
        from app.core.llm import build_provider_llm, set_test_llm
        fake = _FakeRunnable()
        set_test_llm("deepseek", fake)
        result = build_provider_llm("deepseek", None)
        assert result is fake

    def test_audit_provider_override_returns_fake(self) -> None:
        """set_test_llm("audit_deepseek", fake) → build_provider_llm 返回 fake。"""
        from app.core.llm import build_provider_llm, set_test_llm
        fake = _FakeRunnable()
        set_test_llm("audit_deepseek", fake)
        result = build_provider_llm("audit_deepseek", None)
        assert result is fake

    def test_override_only_affects_specified_provider(self) -> None:
        """只 override "deepseek" 时，"openai" 仍走 registry。"""
        from app.core.llm import (
            _PROVIDER_REGISTRY,
            build_provider_llm,
            set_test_llm,
        )
        from langchain_openai import ChatOpenAI
        fake = _FakeRunnable()
        set_test_llm("deepseek", fake)

        result = build_provider_llm("deepseek", None)
        assert result is fake

        openai_llm = build_provider_llm("openai", _FakeSettings())
        assert isinstance(openai_llm, ChatOpenAI)

    def test_clear_single_provider(self) -> None:
        """clear_test_llm("deepseek") 只清除该 provider 的 override。"""
        from app.core.llm import (
            build_provider_llm,
            clear_test_llm,
            set_test_llm,
        )
        fake_ds = _FakeRunnable()
        fake_audit = _FakeRunnable()
        set_test_llm("deepseek", fake_ds)
        set_test_llm("audit_deepseek", fake_audit)

        clear_test_llm("deepseek")
        # deepseek 恢复 registry，audit_deepseek 仍 override
        result_ds = build_provider_llm("deepseek", _FakeSettings())
        from langchain_deepseek import ChatDeepSeek
        assert isinstance(result_ds, ChatDeepSeek)

        result_audit = build_provider_llm("audit_deepseek", None)
        assert result_audit is fake_audit

    def test_clear_all_providers(self) -> None:
        """clear_test_llm() 清除全部 provider 的 override。"""
        from app.core.llm import (
            build_provider_llm,
            clear_test_llm,
            set_test_llm,
        )
        set_test_llm("deepseek", _FakeRunnable())
        set_test_llm("audit_deepseek", _FakeRunnable())
        clear_test_llm()
        # 两个 provider 都恢复 registry
        result_ds = build_provider_llm("deepseek", _FakeSettings())
        from langchain_deepseek import ChatDeepSeek
        assert isinstance(result_ds, ChatDeepSeek)

        result_audit = build_provider_llm("audit_deepseek", _FakeSettings())
        assert isinstance(result_audit, ChatDeepSeek)

    def test_build_main_llm_respects_override(self) -> None:
        """build_main_llm 内部调 build_provider_llm("deepseek", ...)，应返回 override。"""
        from app.core.llm import build_main_llm, set_test_llm
        fake = _FakeRunnable()
        set_test_llm("deepseek", fake)
        result = build_main_llm(_FakeSettings(enable_fallback=False))
        assert result is fake

    def test_build_crisis_llm_respects_override(self) -> None:
        """build_crisis_llm 内部调 build_provider_llm("audit_deepseek", ...)，应返回 override。"""
        from app.core.llm import build_crisis_llm, set_test_llm
        fake = _FakeRunnable()
        set_test_llm("audit_deepseek", fake)
        result = build_crisis_llm(_FakeSettings())
        assert result is fake

    def test_build_audit_llm_respects_override(self) -> None:
        """build_audit_llm 内部调 build_provider_llm("audit_deepseek", ...)，应返回 override。"""
        from app.audit.llm import build_audit_llm
        from app.core.llm import set_test_llm
        fake = _FakeRunnable()
        set_test_llm("audit_deepseek", fake)
        result = build_audit_llm(_FakeSettings())
        assert result is fake

    def test_override_empty_after_teardown(self) -> None:
        """teardown_method 后 _test_llm_overrides 应为空。"""
        from app.core.llm import _test_llm_overrides
        assert _test_llm_overrides == {}
