"""L1 httpx 原生层探针。

覆盖 F1-F5 × DS-native + BL-compat = 14 用例。

⚠️ 探针原则：捕获证据而非断言"能跑通"。
HTTP 401/400 等错误也被 artifact 记录，供 REPORT.md 分析。
"""
from __future__ import annotations

import json

import pytest

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


def _build_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_MESSAGE},
        {"role": "user", "content": USER_MESSAGE},
    ]


def _build_thinking_body(
    thinking_mode: str,
    base: dict[str, object],
) -> dict[str, object]:
    if thinking_mode == "enabled":
        base["thinking"] = {"type": "enabled", "reasoning_effort": "high"}
    elif thinking_mode == "disabled":
        base["thinking"] = {"type": "disabled"}
    return base


async def _do_request(
    builder: ArtifactBuilder,
    cfg: dict[str, str],
    body: dict[str, object],
    *,
    stream: bool = False,
) -> None:
    """发一次 POST 到 /chat/completions，记录请求/响应到 artifact。"""
    url = f"{cfg['base_url']}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}

    if stream:
        client = builder.make_http_client()
        chunks: list[dict[str, object]] = []
        status = 0
        resp_headers: dict[str, str] = {}

        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                status = resp.status_code
                resp_headers = dict(resp.headers)
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        chunks.append(json.loads(data_str))
        except Exception as exc:
            builder.record_response(0, {}, {"error": repr(exc)})
            builder.set_parsed_output({"error": repr(exc)})
            builder.save()
            return

        builder.record_response(status, resp_headers, chunks)

        content = ""
        reasoning_content = ""
        finish_reason: str | None = None
        usage: dict[str, object] | None = None
        for c in chunks:
            choices = c.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                if delta.get("content"):
                    content += delta["content"]
                if delta.get("reasoning_content"):
                    reasoning_content += delta["reasoning_content"]
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]
            if c.get("usage"):
                usage = c["usage"]

        builder.set_parsed_output({
            "content": content,
            "reasoning_content": reasoning_content,
            "finish_reason": finish_reason,
            "usage": usage,
        })
        builder.save()

        if status == 200:
            assert content, "streaming content 为空"
    else:
        client = builder.make_http_client()
        try:
            resp = await client.post(url, json=body, headers=headers)
            resp_data = resp.json() if resp.status_code == 200 else {"http_error": resp.status_code, "body": resp.text}
        except Exception as exc:
            builder.record_response(0, {}, {"error": repr(exc)})
            builder.set_parsed_output({"error": repr(exc)})
            builder.save()
            return

        builder.record_response(resp.status_code, dict(resp.headers), resp_data)

        if resp.status_code == 200:
            choice = resp_data["choices"][0]
            msg = choice["message"]
            builder.set_parsed_output({
                "content": msg.get("content"),
                "reasoning_content": msg.get("reasoning_content"),
                "tool_calls": [
                    {"name": tc["function"]["name"], "arguments": json.loads(tc["function"]["arguments"])}
                    for tc in (msg.get("tool_calls") or [])
                ],
                "finish_reason": choice["finish_reason"],
                "usage": resp_data.get("usage"),
            })
        else:
            builder.set_parsed_output({"http_error": resp.status_code, "body": resp_data})
        builder.save()


# ── F1: 流式 + 无工具 ─────────────────────────────────
async def test_l1_f1_streaming_ds(ds_config: dict[str, str]) -> None:
    body: dict[str, object] = {
        "model": ds_config["model"],
        "messages": _build_messages(),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    _build_thinking_body("enabled", body)
    builder = ArtifactBuilder("L1", "ds-native", "httpx", "F1-streaming", "default")
    await _do_request(builder, ds_config, body, stream=True)


async def test_l1_f1_streaming_bl(bl_config: dict[str, str]) -> None:
    body: dict[str, object] = {
        "model": bl_config["model"],
        "messages": _build_messages(),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    _build_thinking_body("enabled", body)
    builder = ArtifactBuilder("L1", "bl-compat", "httpx", "F1-streaming", "default")
    await _do_request(builder, bl_config, body, stream=True)


# ── F2: 非流式 + 工具 + tool_choice="required"(="any") ─
async def test_l1_f2_any_ds(ds_config: dict[str, str]) -> None:
    body: dict[str, object] = {
        "model": ds_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": "required",  # "any"→"required" per OpenAI protocol
        "stream": False,
    }
    _build_thinking_body("enabled", body)
    builder = ArtifactBuilder("L1", "ds-native", "httpx", "F2-tool_choice=any", "enabled")
    await _do_request(builder, ds_config, body)


async def test_l1_f2_any_bl(bl_config: dict[str, str]) -> None:
    body: dict[str, object] = {
        "model": bl_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": "required",
        "stream": False,
    }
    _build_thinking_body("enabled", body)
    builder = ArtifactBuilder("L1", "bl-compat", "httpx", "F2-tool_choice=any", "enabled")
    await _do_request(builder, bl_config, body)


# ── F3: 非流式 + 工具 + tool_choice="auto" ────────────
async def test_l1_f3_auto_ds(ds_config: dict[str, str]) -> None:
    body: dict[str, object] = {
        "model": ds_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": "auto",
        "stream": False,
    }
    _build_thinking_body("enabled", body)
    builder = ArtifactBuilder("L1", "ds-native", "httpx", "F3-tool_choice=auto", "enabled")
    await _do_request(builder, ds_config, body)


async def test_l1_f3_auto_bl(bl_config: dict[str, str]) -> None:
    body: dict[str, object] = {
        "model": bl_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": "auto",
        "stream": False,
    }
    _build_thinking_body("enabled", body)
    builder = ArtifactBuilder("L1", "bl-compat", "httpx", "F3-tool_choice=auto", "enabled")
    await _do_request(builder, bl_config, body)


# ── F4: 非流式 + 指定 function ────────────────────────
async def test_l1_f4_specific_ds(ds_config: dict[str, str]) -> None:
    body: dict[str, object] = {
        "model": ds_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "audit_output"}},
        "stream": False,
    }
    _build_thinking_body("enabled", body)
    builder = ArtifactBuilder("L1", "ds-native", "httpx", "F4-tool_choice=specific", "enabled")
    await _do_request(builder, ds_config, body)


async def test_l1_f4_specific_bl(bl_config: dict[str, str]) -> None:
    body: dict[str, object] = {
        "model": bl_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "audit_output"}},
        "stream": False,
    }
    _build_thinking_body("enabled", body)
    builder = ArtifactBuilder("L1", "bl-compat", "httpx", "F4-tool_choice=specific", "enabled")
    await _do_request(builder, bl_config, body)


# ── F5: 思考模式三态 ──────────────────────────────────
async def _f5_thinking(
    provider_label: str,
    cfg: dict[str, str],
    thinking: str,
) -> None:
    body: dict[str, object] = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": "请简单介绍一下你自己。"}],
        "stream": False,
    }
    _build_thinking_body(thinking, body)
    builder = ArtifactBuilder("L1", provider_label, "httpx", "F5-thinking", thinking)
    await _do_request(builder, cfg, body)


async def test_l1_f5_thinking_ds_enabled(ds_config: dict[str, str]) -> None:
    await _f5_thinking("ds-native", ds_config, "enabled")

async def test_l1_f5_thinking_ds_disabled(ds_config: dict[str, str]) -> None:
    await _f5_thinking("ds-native", ds_config, "disabled")

async def test_l1_f5_thinking_ds_notset(ds_config: dict[str, str]) -> None:
    await _f5_thinking("ds-native", ds_config, "notset")

async def test_l1_f5_thinking_bl_enabled(bl_config: dict[str, str]) -> None:
    await _f5_thinking("bl-compat", bl_config, "enabled")

async def test_l1_f5_thinking_bl_disabled(bl_config: dict[str, str]) -> None:
    await _f5_thinking("bl-compat", bl_config, "disabled")

async def test_l1_f5_thinking_bl_notset(bl_config: dict[str, str]) -> None:
    await _f5_thinking("bl-compat", bl_config, "notset")
