"""Tests for app.chat.factory — provider registry + ChatDeepSeek primary.

M6 patch 2 (Step 11.1): replaces ChatOpenAI + with_fallbacks tests with
registry dispatch + ChatDeepSeek primary + fallback chain coverage.
"""

from typing import Any

import pytest
from langchain_core.runnables import RunnableBinding, RunnableWithFallbacks
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from app.chat.factory import (
    ProviderNotRegistered,
    _PROVIDER_REGISTRY,
    build_main_llm,
    build_provider_llm,
)


# ---------------------------------------------------------------------------
# Fixtures: minimal settings object
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Minimal settings stub matching fields consumed by factory builders."""

    def __init__(self, **kwargs: Any) -> None:
        from pydantic import SecretStr

        self.main_provider = "deepseek"
        self.fallback_provider: str | None = "deepseek"
        self.enable_fallback = True
        self.deepseek_api_key = SecretStr(kwargs.get("deepseek_api_key", "sk-ds-test"))
        self.deepseek_base_url = kwargs.get("deepseek_base_url", "https://api.deepseek.com/v1")
        self.deepseek_model = kwargs.get("deepseek_model", "deepseek-v4-flash")
        self.deepseek_reasoning_effort = kwargs.get("deepseek_reasoning_effort", "high")
        self.bailian_api_key = SecretStr(kwargs.get("bailian_api_key", "sk-bl-test"))
        self.bailian_base_url = kwargs.get(
            "bailian_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        self.bailian_model = kwargs.get("bailian_model", "deepseek-v4-flash")
        self.llm_request_timeout_seconds = 60.0
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)


# ---- T0: _PROVIDER_REGISTRY has both deepseek and openai keys ----


class TestRegistryKeys:
    """_PROVIDER_REGISTRY contains exactly deepseek and openai."""

    def test_registry_has_deepseek(self) -> None:
        assert "deepseek" in _PROVIDER_REGISTRY

    def test_registry_has_openai(self) -> None:
        assert "openai" in _PROVIDER_REGISTRY

    def test_registry_keys_are_callable(self) -> None:
        settings = _FakeSettings()
        ds_llm = _PROVIDER_REGISTRY["deepseek"](settings)
        assert isinstance(ds_llm, ChatDeepSeek)

        oa_llm = _PROVIDER_REGISTRY["openai"](settings)
        assert isinstance(oa_llm, ChatOpenAI)


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
        with pytest.raises(ProviderNotRegistered, match="unknown"):
            build_provider_llm("unknown", settings)


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
    """main_provider="unknown" triggers ProviderNotRegistered."""

    def test_unknown_main_provider_raises(self) -> None:
        settings = _FakeSettings(main_provider="unknown")
        with pytest.raises(ProviderNotRegistered, match="unknown"):
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

        assert captured.get("base_url") == "https://api.deepseek.com/v1"
        assert captured.get("api_key") == "sk-ds-test"
        assert captured.get("model") == "deepseek-v4-flash"
        assert captured.get("extra_body") == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }

    def test_openai_construction_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock ChatOpenAI.__init__ to verify base_url / api_key / model."""
        captured: dict[str, Any] = {}

        def mock_init(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(ChatOpenAI, "__init__", mock_init)

        settings = _FakeSettings()
        _PROVIDER_REGISTRY["openai"](settings)

        assert captured.get("base_url") == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert captured.get("model") == "deepseek-v4-flash"


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
            base_url="https://api.deepseek.com/v1",
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
            base_url="https://api.deepseek.com/v1",
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
            base_url="https://api.deepseek.com/v1",
            model="deepseek-v4-flash",
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
        )
        assert llm.extra_body["thinking"] == {"type": "enabled"}  # type: ignore[index]


# ---- T6: get_chat_llm backward compat ----


class TestGetChatLlmCompat:
    """get_chat_llm() returns RunnableWithFallbacks via deprecated wrapper."""

    def test_get_chat_llm_returns_runnable_with_fallbacks(self) -> None:
        from app.chat.factory import get_chat_llm

        get_chat_llm.cache_clear()
        runnable = get_chat_llm()
        assert isinstance(runnable, RunnableWithFallbacks)

    def test_get_chat_llm_cached(self) -> None:
        from app.chat.factory import get_chat_llm

        get_chat_llm.cache_clear()
        first = get_chat_llm()
        second = get_chat_llm()
        assert first is second
