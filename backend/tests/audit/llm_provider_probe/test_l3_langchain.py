"""L3 LangChain 层探针。

覆盖 F1-F7 × 四组合（ChatDeepSeek/ChatOpenAI × DS-native/BL-compat）。

⚠️ 探针原则：捕获证据而非断言"能跑通"。API 错误也被 artifact 记录。
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APITimeoutError, RateLimitError

from .conftest import (
    ArtifactBuilder,
    SHARED_TOOLS,
    SYSTEM_MESSAGE,
    USER_MESSAGE,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio,
]

_MESSAGES = [
    SystemMessage(content=SYSTEM_MESSAGE),
    HumanMessage(content=USER_MESSAGE),
]

_MOCK_CHAT_RESPONSE = {
    "id": "mock_cmpl",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "deepseek-v4-flash",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": "This is a mock fallback response.",
        },
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

COMBO_IDS = ["DS-CDS", "DS-COAI", "BL-CDS", "BL-COAI"]

COMBO_MAP = {
    "DS-CDS":  ("ds-native", "ChatDeepSeek"),
    "DS-COAI": ("ds-native", "ChatOpenAI"),
    "BL-CDS":  ("bl-compat", "ChatDeepSeek"),
    "BL-COAI": ("bl-compat", "ChatOpenAI"),
}


def _build_llm(
    cfg: dict[str, str],
    wrapper: str,
    http_async_client: httpx.AsyncClient,
    extra_body: dict[str, Any] | None = None,
) -> ChatDeepSeek | ChatOpenAI:
    kwargs: dict[str, Any] = {
        "model": cfg["model"],
        "http_async_client": http_async_client,
        "timeout": 60,
    }
    if extra_body:
        kwargs["extra_body"] = extra_body

    if wrapper == "ChatDeepSeek":
        kwargs["api_key"] = cfg["api_key"]
        kwargs["api_base"] = cfg["base_url"]
        return ChatDeepSeek(**kwargs)  # type: ignore[arg-type]
    else:
        kwargs["api_key"] = cfg["api_key"]
        kwargs["base_url"] = cfg["base_url"]
        return ChatOpenAI(**kwargs)


def _make_thinking_extra_body(thinking: str) -> dict[str, Any] | None:
    if thinking == "enabled":
        return {"thinking": {"type": "enabled", "reasoning_effort": "high"}}
    if thinking == "disabled":
        return {"thinking": {"type": "disabled"}}
    return None


def _parse_tool_calls(msg: Any) -> list[dict[str, Any]]:
    if hasattr(msg, "tool_calls") and msg.tool_calls:
        return [{"name": tc["name"], "arguments": tc["args"]} for tc in msg.tool_calls]
    return []


# ── combo fixture（间接参数化） ────────────────────────
@pytest.fixture
def combo(request: pytest.FixtureRequest, ds_config: dict[str, str], bl_config: dict[str, str]) -> tuple[str, str, dict[str, str]]:
    cid: str = request.param
    provider_label, wrapper = COMBO_MAP[cid]
    cfg = ds_config if provider_label == "ds-native" else bl_config
    return provider_label, wrapper, cfg


# ═══════════════════════════════════════════════════════
# F1: 流式 + 无工具
# ═══════════════════════════════════════════════════════
@pytest.mark.parametrize("combo", COMBO_IDS, indirect=True)
async def test_l3_f1_streaming(combo: tuple) -> None:
    provider_label, wrapper, cfg = combo
    builder = ArtifactBuilder("L3", provider_label, wrapper, "F1-streaming", "enabled")
    raw_client = builder.make_http_client()
    llm = _build_llm(cfg, wrapper, raw_client, _make_thinking_extra_body("enabled"))

    chunks: list[dict[str, Any]] = []
    full_content = ""
    full_reasoning = ""
    finish_reasons: set[str] = set()
    usage: dict[str, Any] | None = None

    try:
        async for chunk in llm.astream(_MESSAGES):  # type: ignore[arg-type]
            chunk_dict = {
                "content": chunk.content,
                "additional_kwargs": dict(chunk.additional_kwargs),
                "response_metadata": dict(chunk.response_metadata),
            }
            chunks.append(chunk_dict)
            if chunk.content:
                full_content += chunk.content
            rc = chunk.additional_kwargs.get("reasoning_content")
            if rc:
                full_reasoning += rc if isinstance(rc, str) else ""
            meta = chunk.additional_kwargs.get("response_metadata", {})
            if isinstance(meta, dict) and meta.get("finish_reason"):
                finish_reasons.add(meta["finish_reason"])
            if chunk.response_metadata.get("finish_reason"):
                finish_reasons.add(chunk.response_metadata["finish_reason"])
            if chunk.usage_metadata:
                usage = dict(chunk.usage_metadata)
    except Exception as exc:
        builder.record_response(0, {}, {"error": repr(exc)})
        builder.set_parsed_output({"error": repr(exc)})
        builder.save()
        return

    builder.record_response(200, {}, chunks)
    builder.set_parsed_output({
        "content": full_content,
        "reasoning_content": full_reasoning,
        "finish_reason": list(finish_reasons),
        "usage": usage,
    })
    builder.save()

    assert full_content, "streaming content 为空"
    assert "stop" in finish_reasons, f"无 stop finish_reason: {finish_reasons}"


# ═══════════════════════════════════════════════════════
# F2-F4: 非流式 + 工具（统一错误处理）
# ═══════════════════════════════════════════════════════
async def _tool_invoke(
    builder: ArtifactBuilder,
    llm: Any,
    tool_choice: Any,
) -> dict[str, Any]:
    """调用 bind_tools + ainvoke，出错时记录到 artifact 不抛。"""
    builder.set_langchain_input({
        "tool_choice_arg": tool_choice,
        "tools_count": len(SHARED_TOOLS),
    })
    bound = llm.bind_tools(SHARED_TOOLS, tool_choice=tool_choice)  # type: ignore[arg-type]
    try:
        response = await bound.ainvoke(_MESSAGES)  # type: ignore[arg-type]
    except Exception as exc:
        builder.record_response(0, {}, {"error": repr(exc)})
        builder.set_parsed_output({"error": repr(exc)})
        builder.save()
        return {"error": repr(exc)}

    tool_calls = _parse_tool_calls(response)
    builder.record_response(200, {}, {"tool_calls": tool_calls})
    builder.set_parsed_output({
        "content": response.content,
        "reasoning_content": response.additional_kwargs.get("reasoning_content"),
        "tool_calls": tool_calls,
        "finish_reason": response.response_metadata.get("finish_reason"),
        "usage": response.response_metadata.get("token_usage"),
    })
    builder.save()
    return {"ok": True, "tool_calls": tool_calls}


@pytest.mark.parametrize("combo", COMBO_IDS, indirect=True)
async def test_l3_f2_tool_any(combo: tuple) -> None:
    provider_label, wrapper, cfg = combo
    builder = ArtifactBuilder("L3", provider_label, wrapper, "F2-tool_choice=any", "enabled")
    raw_client = builder.make_http_client()
    llm = _build_llm(cfg, wrapper, raw_client, _make_thinking_extra_body("enabled"))
    result = await _tool_invoke(builder, llm, "any")


@pytest.mark.parametrize("combo", COMBO_IDS, indirect=True)
async def test_l3_f3_tool_auto(combo: tuple) -> None:
    provider_label, wrapper, cfg = combo
    builder = ArtifactBuilder("L3", provider_label, wrapper, "F3-tool_choice=auto", "enabled")
    raw_client = builder.make_http_client()
    llm = _build_llm(cfg, wrapper, raw_client, _make_thinking_extra_body("enabled"))
    result = await _tool_invoke(builder, llm, "auto")


@pytest.mark.parametrize("combo", COMBO_IDS, indirect=True)
async def test_l3_f4_tool_specific(combo: tuple) -> None:
    provider_label, wrapper, cfg = combo
    builder = ArtifactBuilder("L3", provider_label, wrapper, "F4-tool_choice=specific", "enabled")
    raw_client = builder.make_http_client()
    llm = _build_llm(cfg, wrapper, raw_client, _make_thinking_extra_body("enabled"))
    result = await _tool_invoke(builder, llm, {"type": "function", "function": {"name": "audit_output"}})


# ═══════════════════════════════════════════════════════
# F5: 思考模式三态
# ═══════════════════════════════════════════════════════
@pytest.mark.parametrize("combo", COMBO_IDS, indirect=True)
@pytest.mark.parametrize("th", ["enabled", "disabled", "notset"])
async def test_l3_f5_thinking(combo: tuple, th: str) -> None:
    provider_label, wrapper, cfg = combo
    builder = ArtifactBuilder("L3", provider_label, wrapper, "F5-thinking", th)
    raw_client = builder.make_http_client()

    eb = _make_thinking_extra_body(th)
    builder.set_langchain_input({"extra_body": eb})

    llm = _build_llm(cfg, wrapper, raw_client, extra_body=eb)

    try:
        response = await llm.ainvoke([HumanMessage(content="请简单介绍一下你自己。")])  # type: ignore[arg-type]
    except Exception as exc:
        builder.record_response(0, {}, {"error": repr(exc)})
        builder.set_parsed_output({"error": repr(exc)})
        builder.save()
        return

    builder.record_response(200, {}, {})
    builder.set_parsed_output({
        "content": response.content,
        "reasoning_content": response.additional_kwargs.get("reasoning_content"),
        "finish_reason": response.response_metadata.get("finish_reason"),
        "usage": response.response_metadata.get("token_usage"),
    })
    builder.save()

    assert response.content, "response content 为空"


# ═══════════════════════════════════════════════════════
# F6: with_fallbacks 退避 (respx mock)
# ═══════════════════════════════════════════════════════
async def test_l3_f6_fallbacks(
    ds_config: dict[str, str],
    bl_config: dict[str, str],
) -> None:
    builder = ArtifactBuilder("L3", "fallback-test", "ChatDeepSeek", "F6-with_fallbacks", "enabled")

    async with respx.mock(assert_all_mocked=True) as respx_mock:
        primary_url = f"{ds_config['base_url']}/chat/completions"
        respx_mock.post(primary_url).mock(side_effect=httpx.ConnectError("mock primary failure"))
        backup_url = f"{bl_config['base_url']}/chat/completions"
        respx_mock.post(backup_url).respond(status_code=200, json=_MOCK_CHAT_RESPONSE)

        raw_client = builder.make_http_client()
        primary = _build_llm(ds_config, "ChatDeepSeek", raw_client, _make_thinking_extra_body("enabled"))
        backup = _build_llm(bl_config, "ChatDeepSeek", raw_client, _make_thinking_extra_body("enabled"))
        chain = primary.with_fallbacks([backup])  # type: ignore[arg-type]

        response = await chain.ainvoke(_MESSAGES)  # type: ignore[arg-type]

    builder.record_response(200, {}, {"content": response.content})
    builder.set_parsed_output({
        "content": response.content,
        "tool_calls": [],
        "finish_reason": "stop",
    })
    builder.save()

    assert response.content == _MOCK_CHAT_RESPONSE["choices"][0]["message"]["content"], f"fallback 未生效，得到: {response.content}"
    # 至少有一次主端请求 + 一次备端请求
    primary_calls = [e for e in builder.req_entries if primary_url in e["url"]]
    backup_calls = [e for e in builder.req_entries if backup_url in e["url"]]
    assert len(primary_calls) >= 1, f"无主端请求: {len(primary_calls)}"
    assert len(backup_calls) >= 1, f"无备端请求: {len(backup_calls)}"


# ═══════════════════════════════════════════════════════
# F7: with_retry 瞬态重试 (respx mock)
# ═══════════════════════════════════════════════════════
async def test_l3_f7_retry(ds_config: dict[str, str]) -> None:
    builder = ArtifactBuilder("L3", "retry-test", "ChatDeepSeek", "F7-with_retry", "enabled")
    url = f"{ds_config['base_url']}/chat/completions"

    call_index = 0

    async def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_index
        call_index += 1
        if call_index == 1:
            return httpx.Response(
                status_code=429,
                json={"error": {"message": "Rate limit", "type": "rate_limit"}},
            )
        return httpx.Response(status_code=200, json=_MOCK_CHAT_RESPONSE)

    async with respx.mock(assert_all_mocked=True) as respx_mock:
        respx_mock.post(url).mock(side_effect=side_effect)
        raw_client = builder.make_http_client()
        llm = _build_llm(ds_config, "ChatDeepSeek", raw_client, _make_thinking_extra_body("enabled"))
        retryable = llm.with_retry(
            retry_if_exception_type=(RateLimitError, APITimeoutError, APIConnectionError),
            stop_after_attempt=2,
            wait_exponential_jitter=True,
        )
        response = await retryable.ainvoke(_MESSAGES)  # type: ignore[arg-type]

    builder.record_response(200, {}, {"content": response.content})
    builder.set_parsed_output({
        "content": response.content,
        "tool_calls": [],
        "finish_reason": "stop",
    })
    builder.set_langchain_input({
        "retry_if_exception_type": ["RateLimitError", "APITimeoutError", "APIConnectionError"],
        "stop_after_attempt": 2,
        "wait_exponential_jitter": True,
    })
    builder.save()

    assert call_index == 2, f"期望 2 次请求（1次429+1次重试），实际: {call_index}"
    assert response.content == _MOCK_CHAT_RESPONSE["choices"][0]["message"]["content"]
