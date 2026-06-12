"""补1': thinking=enabled 下 tool_choice 穷尽探针。

在 thinking={"type":"enabled"} 前提下，测试 tool_choice="required" 和
tool_choice={function object} 的所有合法写法变体。

矩阵: 2 端点 × 2 tool_choice × (7 L1 + 1 L2 + 1 L3) = 36 用例
"""
from __future__ import annotations

import copy
import json

import pytest

from .conftest import (
    SHARED_TOOLS,
    USER_MESSAGE,
    ArtifactBuilder,
)

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

SYSTEM_CONSTRAINT = "You MUST call the audit_output function. Do not respond with text."

VARIANTS = ["v0", "v1", "v2", "v3", "v4", "v5", "v6"]
TOOL_CHOICES = ["required", "function"]

_MESSAGES_NO_SYS = [{"role": "user", "content": USER_MESSAGE}]


def _clone_tools() -> list[dict]:
    return copy.deepcopy(SHARED_TOOLS)


def _build_variant_body(
    model: str,
    messages: list[dict],
    tools: list[dict],
    tc_key: str,
    variant: str,
) -> dict:
    body: dict = {
        "model": model,
        "messages": copy.deepcopy(messages),
        "tools": tools,
        "stream": False,
    }

    # tool_choice
    if tc_key == "required":
        body["tool_choice"] = "required"
    else:
        body["tool_choice"] = {
            "type": "function",
            "function": {"name": "audit_output"},
        }

    # thinking block
    thinking: dict = {"type": "enabled"}
    if variant in ("v1", "v5", "v6"):
        thinking["reasoning_effort"] = "high"
    body["thinking"] = thinking

    # strict
    if variant in ("v2", "v6"):
        for t in body["tools"]:
            t["function"]["strict"] = True

    # parallel_tool_calls
    if variant in ("v3", "v6"):
        body["parallel_tool_calls"] = False

    # system constraint
    if variant in ("v4", "v5", "v6"):
        body["messages"].insert(
            0, {"role": "system", "content": SYSTEM_CONSTRAINT}
        )

    return body


async def _l1_send(
    cfg: dict,
    body: dict,
    builder: ArtifactBuilder,
) -> None:
    """L1 httpx 原生发送并记录。"""
    url = f"{cfg['base_url']}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    client = builder.make_http_client()
    try:
        resp = await client.post(url, json=body, headers=headers)
        status = resp.status_code
        try:
            data = resp.json()
        except Exception:
            data = {"raw_text": resp.text}
    except Exception as exc:
        builder.record_response(0, {}, {"error": repr(exc)})
        builder.set_parsed_output({"error": repr(exc)})
        builder.save()
        return

    builder.record_response(status, dict(resp.headers), data)
    if status == 200:
        choice = data["choices"][0]
        msg = choice["message"]
        builder.set_parsed_output({
            "content": msg.get("content"),
            "reasoning_content": msg.get("reasoning_content"),
            "tool_calls": [
                {"name": tc["function"]["name"], "arguments": json.loads(tc["function"]["arguments"])}
                for tc in (msg.get("tool_calls") or [])
            ],
            "finish_reason": choice["finish_reason"],
            "usage": data.get("usage"),
        })
    else:
        builder.set_parsed_output({
            "http_error": status,
            "error_body": data,
        })
    builder.save()


# ── L1: 7 个写法变体 × 2 端点 × 2 tool_choice = 28 ─────
@pytest.mark.parametrize("endpoint", ["ds", "bl"])
@pytest.mark.parametrize("tc", TOOL_CHOICES)
@pytest.mark.parametrize("variant", VARIANTS)
async def test_f2prime_l1(endpoint: str, tc: str, variant: str, ds_config: dict, bl_config: dict) -> None:
    cfg = ds_config if endpoint == "ds" else bl_config
    provider_label = "ds-native" if endpoint == "ds" else "bl-compat"
    builder = ArtifactBuilder("L1", provider_label, "httpx", f"F2prime-{tc}", f"L1{variant}")

    body = _build_variant_body(cfg["model"], _MESSAGES_NO_SYS, _clone_tools(), tc, variant)
    await _l1_send(cfg, body, builder)


# ── L2: OpenAI SDK 基线 × 2 端点 × 2 tool_choice = 4 ────
@pytest.mark.parametrize("endpoint", ["ds", "bl"])
@pytest.mark.parametrize("tc", TOOL_CHOICES)
async def test_f2prime_l2(endpoint: str, tc: str, ds_config: dict, bl_config: dict) -> None:
    from openai import AsyncOpenAI

    cfg = ds_config if endpoint == "ds" else bl_config
    provider_label = "ds-native" if endpoint == "ds" else "bl-compat"
    builder = ArtifactBuilder("L2", provider_label, "AsyncOpenAI", f"F2prime-{tc}", "L2")

    raw = builder.make_http_client()
    client = AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], http_client=raw)

    tc_val: str | dict = "required" if tc == "required" else {"type": "function", "function": {"name": "audit_output"}}

    try:
        response = await client.chat.completions.create(
            model=cfg["model"],
            messages=_MESSAGES_NO_SYS,
            tools=SHARED_TOOLS,
            tool_choice=tc_val,
            extra_body={"thinking": {"type": "enabled"}},
        )
        data = response.model_dump()
        builder.record_response(200, {}, data)
        choice = data["choices"][0]
        msg = choice["message"]
        builder.set_parsed_output({
            "content": msg.get("content"),
            "reasoning_content": msg.get("reasoning_content"),
            "tool_calls": [
                {"name": tc_["function"]["name"], "arguments": json.loads(tc_["function"]["arguments"])}
                for tc_ in (msg.get("tool_calls") or [])
            ],
            "finish_reason": choice["finish_reason"],
            "usage": data.get("usage"),
        })
    except Exception as exc:
        builder.record_response(0, {}, {"error": repr(exc)})
        builder.set_parsed_output({"error": repr(exc)})
    builder.save()


# ── L3: LangChain 基线 × 2 端点 × 2 tool_choice = 4 ────
@pytest.mark.parametrize("endpoint", ["ds", "bl"])
@pytest.mark.parametrize("tc", TOOL_CHOICES)
async def test_f2prime_l3(endpoint: str, tc: str, ds_config: dict, bl_config: dict) -> None:
    from langchain_core.messages import HumanMessage
    from langchain_deepseek import ChatDeepSeek

    cfg = ds_config if endpoint == "ds" else bl_config
    provider_label = "ds-native" if endpoint == "ds" else "bl-compat"
    builder = ArtifactBuilder("L3", provider_label, "ChatDeepSeek", f"F2prime-{tc}", "L3")

    raw = builder.make_http_client()
    llm = ChatDeepSeek(
        api_key=cfg["api_key"],
        api_base=cfg["base_url"],
        model=cfg["model"],
        http_async_client=raw,
        timeout=60,
        extra_body={"thinking": {"type": "enabled"}},
    )

    tc_val: str | dict = "required" if tc == "required" else {"type": "function", "function": {"name": "audit_output"}}

    builder.set_langchain_input({
        "tool_choice_arg": tc_val,
        "tools_count": len(SHARED_TOOLS),
        "extra_body": {"thinking": {"type": "enabled"}},
    })

    try:
        bound = llm.bind_tools(SHARED_TOOLS, tool_choice=tc_val)
        response = await bound.ainvoke([HumanMessage(content=USER_MESSAGE)])
        tool_calls = []
        if hasattr(response, "tool_calls") and response.tool_calls:
            tool_calls = [{"name": tc_["name"], "arguments": tc_["args"]} for tc_ in response.tool_calls]
        builder.record_response(200, {}, {"tool_calls": tool_calls})
        builder.set_parsed_output({
            "content": response.content,
            "reasoning_content": response.additional_kwargs.get("reasoning_content"),
            "tool_calls": tool_calls,
            "finish_reason": response.response_metadata.get("finish_reason"),
            "usage": response.response_metadata.get("token_usage"),
        })
    except Exception as exc:
        builder.record_response(0, {}, {"error": repr(exc)})
        builder.set_parsed_output({"error": repr(exc)})
    builder.save()
