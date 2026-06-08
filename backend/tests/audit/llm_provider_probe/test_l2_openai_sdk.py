"""L2 OpenAI SDK 层探针。

使用 openai.AsyncOpenAI 代替裸 httpx，验证 SDK 层面的字段处理。
覆盖 F1-F5 × DS-native + BL-compat = 14 用例。

⚠️ 探针原则：捕获证据而非断言"能跑通"。HTTP 400/401 等错误也被 artifact 记录。
"""
from __future__ import annotations

import json

import pytest

from .conftest import (
    SHARED_TOOLS,
    SYSTEM_MESSAGE,
    USER_MESSAGE,
    ArtifactBuilder,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio,
]


def _make_client(
    cfg: dict[str, str],
    builder: ArtifactBuilder,
):
    """创建挂载了事件钩子的 AsyncOpenAI 客户端。"""
    from openai import AsyncOpenAI

    raw_client = builder.make_http_client()
    return AsyncOpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        http_client=raw_client,
    )


def _build_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_MESSAGE},
        {"role": "user", "content": USER_MESSAGE},
    ]


async def _do_nonstream(
    builder: ArtifactBuilder,
    client,
    kwargs: dict,
) -> None:
    """非流式调用并记录结果，错误也被捕获进 artifact。"""
    try:
        response = await client.chat.completions.create(**kwargs)
    except Exception as exc:
        builder.record_response(0, {}, {"error": repr(exc)})
        builder.set_parsed_output({"error": repr(exc)})
        builder.save()
        return

    data = response.model_dump()
    builder.record_response(200, {}, data)

    if "tools" in kwargs:
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
        choice = data["choices"][0]
        msg = choice["message"]
        builder.set_parsed_output({
            "content": msg.get("content"),
            "reasoning_content": msg.get("reasoning_content"),
            "finish_reason": choice["finish_reason"],
            "usage": data.get("usage"),
        })
    builder.save()


# ── F1: 流式 + 无工具 ─────────────────────────────────
async def test_l2_f1_streaming_ds(ds_config: dict[str, str]) -> None:
    builder = ArtifactBuilder("L2", "ds-native", "AsyncOpenAI", "F1-streaming", "default")
    client = _make_client(ds_config, builder)

    try:
        response = await client.chat.completions.create(
            model=ds_config["model"],
            messages=_build_messages(),
            stream=True,
            stream_options={"include_usage": True},
            extra_body={"thinking": {"type": "enabled", "reasoning_effort": "high"}},
        )
    except Exception as exc:
        builder.record_response(0, {}, {"error": repr(exc)})
        builder.set_parsed_output({"error": repr(exc)})
        builder.save()
        return

    chunks: list[dict[str, object]] = []
    content = ""
    reasoning_content = ""
    finish_reason: str | None = None
    usage: dict[str, object] | None = None

    async for chunk in response:
        cd = chunk.model_dump()
        chunks.append(cd)
        choice = chunk.choices[0] if chunk.choices else None
        if choice and choice.delta:
            if choice.delta.content:
                content += choice.delta.content
            rc = getattr(choice.delta, "reasoning_content", None)
            if rc:
                reasoning_content += rc
            if choice.finish_reason:
                finish_reason = choice.finish_reason
        if chunk.usage:
            usage = chunk.usage.model_dump()

    builder.record_response(200, {}, chunks)
    builder.set_parsed_output({
        "content": content,
        "reasoning_content": reasoning_content,
        "finish_reason": finish_reason,
        "usage": usage,
    })
    builder.save()

    assert finish_reason == "stop", f"finish_reason={finish_reason}"
    assert content, "streaming content 为空"


async def test_l2_f1_streaming_bl(bl_config: dict[str, str]) -> None:
    builder = ArtifactBuilder("L2", "bl-compat", "AsyncOpenAI", "F1-streaming", "default")
    client = _make_client(bl_config, builder)

    try:
        response = await client.chat.completions.create(
            model=bl_config["model"],
            messages=_build_messages(),
            stream=True,
            stream_options={"include_usage": True},
            extra_body={"thinking": {"type": "enabled", "reasoning_effort": "high"}},
        )
    except Exception as exc:
        builder.record_response(0, {}, {"error": repr(exc)})
        builder.set_parsed_output({"error": repr(exc)})
        builder.save()
        return

    chunks = []
    content = ""
    reasoning_content = ""
    finish_reason: str | None = None
    usage = None

    async for chunk in response:
        cd = chunk.model_dump()
        chunks.append(cd)
        choice = chunk.choices[0] if chunk.choices else None
        if choice and choice.delta:
            if choice.delta.content:
                content += choice.delta.content
            rc = getattr(choice.delta, "reasoning_content", None)
            if rc:
                reasoning_content += rc
            if choice.finish_reason:
                finish_reason = choice.finish_reason
        if chunk.usage:
            usage = chunk.usage.model_dump()

    builder.record_response(200, {}, chunks)
    builder.set_parsed_output({
        "content": content,
        "reasoning_content": reasoning_content,
        "finish_reason": finish_reason,
        "usage": usage,
    })
    builder.save()

    assert finish_reason == "stop"
    assert content


# ── F2: 非流式 + 工具 + tool_choice="required"(="any") ─
async def test_l2_f2_any_ds(ds_config: dict[str, str]) -> None:
    builder = ArtifactBuilder("L2", "ds-native", "AsyncOpenAI", "F2-tool_choice=any", "enabled")
    client = _make_client(ds_config, builder)
    await _do_nonstream(builder, client, {
        "model": ds_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": "required",
        "extra_body": {"thinking": {"type": "enabled", "reasoning_effort": "high"}},
    })


async def test_l2_f2_any_bl(bl_config: dict[str, str]) -> None:
    builder = ArtifactBuilder("L2", "bl-compat", "AsyncOpenAI", "F2-tool_choice=any", "enabled")
    client = _make_client(bl_config, builder)
    await _do_nonstream(builder, client, {
        "model": bl_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": "required",
        "extra_body": {"thinking": {"type": "enabled", "reasoning_effort": "high"}},
    })


# ── F3: 非流式 + 工具 + tool_choice="auto" ────────────
async def test_l2_f3_auto_ds(ds_config: dict[str, str]) -> None:
    builder = ArtifactBuilder("L2", "ds-native", "AsyncOpenAI", "F3-tool_choice=auto", "enabled")
    client = _make_client(ds_config, builder)
    await _do_nonstream(builder, client, {
        "model": ds_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": "auto",
        "extra_body": {"thinking": {"type": "enabled", "reasoning_effort": "high"}},
    })


async def test_l2_f3_auto_bl(bl_config: dict[str, str]) -> None:
    builder = ArtifactBuilder("L2", "bl-compat", "AsyncOpenAI", "F3-tool_choice=auto", "enabled")
    client = _make_client(bl_config, builder)
    await _do_nonstream(builder, client, {
        "model": bl_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": "auto",
        "extra_body": {"thinking": {"type": "enabled", "reasoning_effort": "high"}},
    })


# ── F4: 非流式 + 指定 function ────────────────────────
async def test_l2_f4_specific_ds(ds_config: dict[str, str]) -> None:
    builder = ArtifactBuilder("L2", "ds-native", "AsyncOpenAI", "F4-tool_choice=specific", "enabled")
    client = _make_client(ds_config, builder)
    await _do_nonstream(builder, client, {
        "model": ds_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "audit_output"}},
        "extra_body": {"thinking": {"type": "enabled", "reasoning_effort": "high"}},
    })


async def test_l2_f4_specific_bl(bl_config: dict[str, str]) -> None:
    builder = ArtifactBuilder("L2", "bl-compat", "AsyncOpenAI", "F4-tool_choice=specific", "enabled")
    client = _make_client(bl_config, builder)
    await _do_nonstream(builder, client, {
        "model": bl_config["model"],
        "messages": _build_messages(),
        "tools": SHARED_TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "audit_output"}},
        "extra_body": {"thinking": {"type": "enabled", "reasoning_effort": "high"}},
    })


# ── F5: 思考模式三态 ──────────────────────────────────
async def _f5_thinking(
    provider_label: str,
    cfg: dict[str, str],
    thinking: str,
) -> None:
    builder = ArtifactBuilder("L2", provider_label, "AsyncOpenAI", "F5-thinking", thinking)
    client = _make_client(cfg, builder)

    extra_body: dict[str, object] = {}
    if thinking == "enabled":
        extra_body["thinking"] = {"type": "enabled", "reasoning_effort": "high"}
    elif thinking == "disabled":
        extra_body["thinking"] = {"type": "disabled"}

    kwargs: dict = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": "请简单介绍一下你自己。"}],
    }
    if extra_body:
        kwargs["extra_body"] = extra_body

    await _do_nonstream(builder, client, kwargs)


async def test_l2_f5_thinking_ds_enabled(ds_config: dict[str, str]) -> None:
    await _f5_thinking("ds-native", ds_config, "enabled")

async def test_l2_f5_thinking_ds_disabled(ds_config: dict[str, str]) -> None:
    await _f5_thinking("ds-native", ds_config, "disabled")

async def test_l2_f5_thinking_ds_notset(ds_config: dict[str, str]) -> None:
    await _f5_thinking("ds-native", ds_config, "notset")

async def test_l2_f5_thinking_bl_enabled(bl_config: dict[str, str]) -> None:
    await _f5_thinking("bl-compat", bl_config, "enabled")

async def test_l2_f5_thinking_bl_disabled(bl_config: dict[str, str]) -> None:
    await _f5_thinking("bl-compat", bl_config, "disabled")

async def test_l2_f5_thinking_bl_notset(bl_config: dict[str, str]) -> None:
    await _f5_thinking("bl-compat", bl_config, "notset")
