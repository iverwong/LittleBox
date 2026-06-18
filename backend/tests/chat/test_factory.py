"""Tests for app.core.llm — role-driven factory + adapter（Step 7 重写）。

Step 7 收口:消化 Step 0 清单的 _PROVIDER_REGISTRY / _PUBLIC_KEYS / _parse_key
相关 26 个旧断言,改为 Role 驱动断言 + Given/When/Then 风格。

设计:
- 不引入 _FakeRunnable(_FakeRunnable 唯一保留在 tests/core/test_llm_topology.py);
  本文件用真 ChatDeepSeek 实例 + mock __init__ 捕获 kwargs,或简单 object() 验
  注入缝短路
- audit/llm 装配链覆盖(主备两端各 bind 3 工具)在 tests/audit/test_audit_llm.py
  (Step 6 新增),本文件不复测
- T7 (build_audit_llm HTTP retry/fallback) 保留,URL 改 ENDPOINTS 表
- T5b (ChatDeepSeek extra_body 字段存活性) 保留
- 注入缝验证:set_test_llm(Role, fake) → build_role_primary(Role, ...) is fake

隔离铁律:本文件为纯单测,无需 DB/Redis fixture;respx 在 T7 用做 HTTP 层 mock,
禁止真实连接 / subprocess / flushdb。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core.llm import (
    build_compression_llm,
    build_crisis_llm,
    build_main_llm,
    build_role_fallback,
    build_role_primary,
)
from app.core.llm_topology import (
    ENDPOINTS,
    EndpointName,
    ModelProfileNotRegisteredError,
    Role,
    resolve_profile,
)
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableWithFallbacks
from langchain_deepseek import ChatDeepSeek

# ---------------------------------------------------------------------------
# Fixtures: minimal settings object
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Minimal settings stub matching fields consumed by factory builders.

    字段范围:仅 _build_binding 通过 `ep.api_key(settings).get_secret_value()`
    读到 deepseek_api_key / bailian_api_key;其它 settings 字段不参与。
    """

    def __init__(self, **kwargs: Any) -> None:
        from pydantic import SecretStr

        self.deepseek_api_key = SecretStr(kwargs.get("deepseek_api_key", "sk-ds-test"))
        self.bailian_api_key = SecretStr(kwargs.get("bailian_api_key", "sk-bl-test"))


# ---------------------------------------------------------------------------
# T0: resolve_profile
# ---------------------------------------------------------------------------


class TestResolveProfile:
    """resolve_profile 按 family 前缀匹配 ModelProfile,未注册抛错。"""

    def test_deepseek_v4_flash_hits(self) -> None:
        """Given model="deepseek-v4-flash" When resolve_profile Then 命中 deepseek-v4 档。"""
        profile = resolve_profile("deepseek-v4-flash")
        assert profile.family == "deepseek-v4"

    def test_deepseek_v4_pro_hits(self) -> None:
        """Given model="deepseek-v4-pro" When resolve_profile Then 命中 deepseek-v4 档(同前缀)。"""
        assert resolve_profile("deepseek-v4-pro").family == "deepseek-v4"

    def test_unknown_model_raises(self) -> None:
        """未注册 model 抛错。"""
        with pytest.raises(
            ModelProfileNotRegisteredError, match="qwen-vl"
        ):
            resolve_profile("qwen-vl-max")


# ---------------------------------------------------------------------------
# T1: build_role_primary(走 _adapter_chat_deepseek 装配)
# ---------------------------------------------------------------------------


class TestBuildRolePrimary:
    """build_role_primary 返裸 ChatModel 实例,主端 kwargs 来自 ROLES[role]。"""

    def test_main_returns_chat_deepseek_with_correct_kwargs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """role=MAIN 主端 kwargs 实证(deepseek 端点 + thinking + effort)。"""
        captured: dict[str, Any] = {}

        def mock_init(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(ChatDeepSeek, "__init__", mock_init)

        build_role_primary(Role.MAIN, _FakeSettings())

        assert captured.get("api_base") == "https://api.deepseek.com"
        assert captured.get("api_key") == "sk-ds-test"
        assert captured.get("model") == "deepseek-v4-flash"
        assert captured.get("extra_body") == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        }

    def test_audit_returns_chat_deepseek_with_same_kwargs_as_main(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given role=AUDIT When build_role_primary Then kwargs 与 MAIN 字节等价(今日 ROLES 同)。"""
        captured: dict[str, Any] = {}

        def mock_init(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(ChatDeepSeek, "__init__", mock_init)

        build_role_primary(Role.AUDIT, _FakeSettings())

        assert captured.get("extra_body") == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        }

    def test_compression_has_temperature_0_3_and_no_reasoning_effort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """role=COMPRESSION 主端:temperature=0.3, 无 reasoning_effort, thinking=disabled。"""
        captured: dict[str, Any] = {}

        def mock_init(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(ChatDeepSeek, "__init__", mock_init)

        build_role_primary(Role.COMPRESSION, _FakeSettings())

        assert captured.get("temperature") == 0.3
        # reasoning_effort 不在 extra_body 中(compression 走 None 路径)
        assert "reasoning_effort" not in captured.get("extra_body", {})
        assert captured.get("extra_body", {}).get("thinking") == {"type": "disabled"}


# ---------------------------------------------------------------------------
# T2: build_role_fallback
# ---------------------------------------------------------------------------


class TestBuildRoleFallback:
    """build_role_fallback 返 ROLES[role].fallback 裸实例(不查 override)。"""

    def test_main_fallback_uses_bailian(self) -> None:
        """role=MAIN fallback 走 bailian 端点。"""
        fb = build_role_fallback(Role.MAIN, _FakeSettings())
        assert fb is not None
        assert isinstance(fb, ChatDeepSeek)
        assert fb.api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"  # type: ignore[attr-defined]

    def test_compression_fallback_uses_bailian(self) -> None:
        """Given role=COMPRESSION When build_role_fallback Then 返 ChatDeepSeek@bailian。"""
        fb = build_role_fallback(Role.COMPRESSION, _FakeSettings())
        assert fb is not None
        assert fb.api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# T3: build_main_llm / build_crisis_llm / build_compression_llm
# ---------------------------------------------------------------------------


class TestBuildMainLlm:
    """build_main_llm = _build_role_llm(MAIN):RunnableWithFallbacks(retry=3 + 1 fallback)。"""

    def test_returns_runnable_with_fallbacks(self) -> None:
        """Given settings When build_main_llm Then 返 RunnableWithFallbacks,fallback 数量 = 1。"""
        s = _FakeSettings()
        result = build_main_llm(s)
        assert isinstance(result, RunnableWithFallbacks)
        assert len(result.fallbacks) == 1  # type: ignore[attr-defined]


class TestBuildCrisisLlm:
    """build_crisis_llm 走 Role.MAIN 绑定(关注点 6:crisis 复用 main,不复用 audit)。"""

    def test_uses_main_binding_not_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """crisis 走 MAIN 绑定(关注点 6),非 AUDIT。"""
        from app.core.llm import _build_role_llm

        called_with: list[Role] = []

        def spy(role: Role, settings: Any) -> Any:
            called_with.append(role)
            return _build_role_llm(role, settings)

        monkeypatch.setattr("app.core.llm._build_role_llm", spy)
        build_crisis_llm(_FakeSettings())
        assert called_with == [Role.MAIN], (
            f"crisis 应走 MAIN 绑定(关注点 6),实际 {called_with}"
        )


class TestBuildCompressionLlm:
    """build_compression_llm = _build_role_llm(COMPRESSION):retry=1 + bailian 兜底。"""

    def test_returns_runnable_with_fallbacks(self) -> None:
        """Given settings When build_compression_llm Then 返 RunnableWithFallbacks。"""
        s = _FakeSettings()
        result = build_compression_llm(s)
        assert isinstance(result, RunnableWithFallbacks)
        assert len(result.fallbacks) == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# T5b: ChatDeepSeek extra_body 字段存活性(原文件保留,语义未变)
# ---------------------------------------------------------------------------


class TestChatDeepSeekThinkingParams:
    """ChatDeepSeek 真实例上 extra_body 字段存活性(Step 11.3 实证,本次保留)。"""

    def test_extra_body_survives_construction(self) -> None:
        """ChatDeepSeek(api_base=..., model=..., extra_body={...}) 实例化后 extra_body 字段可读。"""
        llm = ChatDeepSeek(
            api_key="sk-test",
            api_base="https://api.deepseek.com",
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


# ---------------------------------------------------------------------------
# T7: build_audit_llm HTTP retry + fallback(respx mock)
# ---------------------------------------------------------------------------


class TestAuditLlmRetry:
    """build_audit_llm 走 with_retry + with_fallbacks,HTTP 层实证。"""

    @pytest.fixture
    def _no_retry_backoff(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        """拦截 tenacity 退避入口 asyncio.sleep 使重试真实发生但耗时归零。"""
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)
        return sleep_mock

    async def test_primary_retry_on_connect_error_then_success(
        self, _no_retry_backoff: AsyncMock
    ) -> None:
        """主端首 ConnectError, retry 1 次后成功。"""
        import httpx
        import respx
        from app.domain.audit.llm import build_audit_llm

        primary_url = (
            f"{ENDPOINTS[EndpointName.DEEPSEEK].base_url}/chat/completions"
        )

        async with respx.mock(assert_all_mocked=False) as respx_mock:
            route = respx_mock.post(primary_url)
            route.mock(
                side_effect=[
                    httpx.ConnectError("mock connection refused"),
                    httpx.Response(
                        status_code=200,
                        json={
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "测试回复",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                        },
                    ),
                ],
            )

            llm = build_audit_llm(_FakeSettings())
            result = await llm.ainvoke([HumanMessage(content="你好")])
            assert result.content is not None
            assert len(respx_mock.calls) == 2

    async def test_primary_all_fail_uses_fallback(
        self, _no_retry_backoff: AsyncMock
    ) -> None:
        """Given 主端持续 ConnectError + 备端 OK When build_audit_llm.ainvoke Then 切备端成功。"""
        import httpx
        import respx
        from app.domain.audit.llm import build_audit_llm

        primary_url = (
            f"{ENDPOINTS[EndpointName.DEEPSEEK].base_url}/chat/completions"
        )
        fallback_url = (
            f"{ENDPOINTS[EndpointName.BAILIAN].base_url}/chat/completions"
        )

        async with respx.mock(assert_all_mocked=False) as respx_mock:
            primary_route = respx_mock.post(primary_url)
            primary_route.mock(
                side_effect=[
                    httpx.ConnectError("mock connection refused") for _ in range(3)
                ],
            )
            respx_mock.post(fallback_url).respond(
                status_code=200,
                json={
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "备端回复"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

            llm = build_audit_llm(_FakeSettings())
            result = await llm.ainvoke([HumanMessage(content="你好")])
            assert result.content is not None


# ---------------------------------------------------------------------------
# T8: 注入缝 set_test_llm / clear_test_llm 按 Role 键
# ---------------------------------------------------------------------------


class TestInjectionSeamRoleKey:
    """set_test_llm(Role, fake) → build_role_primary(Role, ...) 返 fake(短路 _build_binding)。"""

    def test_set_role_main_short_circuits_primary(self, llm_override: Any) -> None:
        """Given set_test_llm(Role.MAIN, fake) When build_role_primary(MAIN) Then 返 fake。"""
        fake = object()  # 任何对象均可,build_role_primary 直接返回
        llm_override(Role.MAIN, fake)
        result = build_role_primary(Role.MAIN, _FakeSettings())
        assert result is fake

    def test_set_role_audit_short_circuits_primary(self, llm_override: Any) -> None:
        """Given set_test_llm(Role.AUDIT, fake) When build_role_primary(AUDIT) Then 返 fake。"""
        fake = object()
        llm_override(Role.AUDIT, fake)
        result = build_role_primary(Role.AUDIT, _FakeSettings())
        assert result is fake

    def test_set_role_compression_short_circuits_primary(self, llm_override: Any) -> None:
        """set_test_llm(Role.COMPRESSION) 短路 build_role_primary。"""
        fake = object()
        llm_override(Role.COMPRESSION, fake)
        result = build_role_primary(Role.COMPRESSION, _FakeSettings())
        assert result is fake

    def test_set_role_string_raises_type_error(self) -> None:
        """set_test_llm("字符串") 抛 TypeError(关注点 1 isinstance 守卫)。

        漏改字符串 → 立即 TypeError,避免「override 静默失效回落真 LLM」暗坑。
        """
        from app.core.llm import set_test_llm

        with pytest.raises(TypeError, match="仅接受 Role"):
            set_test_llm("deepseek", object())  # type: ignore[arg-type]

    def test_clear_role_string_raises_type_error(self) -> None:
        """clear_test_llm("字符串") 抛 TypeError(对称守卫)。"""
        from app.core.llm import clear_test_llm

        with pytest.raises(TypeError, match="仅接受 Role"):
            clear_test_llm("audit_deepseek")  # type: ignore[arg-type]

    def test_clear_specific_role_keeps_others(self, llm_override: Any) -> None:
        """clear_test_llm(MAIN) 不影响 AUDIT override。"""
        from app.core.llm import clear_test_llm

        fake_main = object()
        fake_audit = object()
        llm_override(Role.MAIN, fake_main)
        llm_override(Role.AUDIT, fake_audit)
        clear_test_llm(Role.MAIN)
        assert build_role_primary(Role.MAIN, _FakeSettings()) is not fake_main
        assert build_role_primary(Role.AUDIT, _FakeSettings()) is fake_audit

    def test_clear_all_clears_everything(self, llm_override: Any) -> None:
        """Given set_test_llm 多次 When clear_test_llm() 无参 Then 全部清空。"""
        from app.core.llm import _test_llm_overrides, clear_test_llm

        llm_override(Role.MAIN, object())
        llm_override(Role.AUDIT, object())
        clear_test_llm()
        assert _test_llm_overrides == {}
