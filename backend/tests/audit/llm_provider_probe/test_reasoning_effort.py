"""补5: reasoning_effort high vs max 透传与生效对比。

对 DS-native 端跑 thinking="enabled" + tool_choice="auto":
  - reasoning_effort="high"
  - reasoning_effort="max"

比较: reasoning_tokens 数量 / reasoning_content 长度 / 整体延迟。
"""
from __future__ import annotations

import time

import pytest

from .conftest import ArtifactBuilder

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


async def _run_reffort(cfg: dict, effort: str, builder: ArtifactBuilder) -> None:
    """发一次非流式 chat，记录 response。"""

    client = builder.make_http_client()
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    url = f"{cfg['base_url']}/chat/completions"

    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": "请详细分析一下中国足球的发展历程和未来前景。"}],
        "stream": False,
        "thinking": {"type": "enabled", "reasoning_effort": effort},
    }

    t0 = time.monotonic()
    try:
        resp = await client.post(url, json=body, headers=headers)
        elapsed = time.monotonic() - t0
        data = resp.json() if resp.status_code == 200 else {"error_body": resp.text}
    except Exception as exc:
        builder.record_response(0, {}, {"error": repr(exc)})
        builder.set_parsed_output({"error": repr(exc), "elapsed_seconds": time.monotonic() - t0})
        builder.save()
        return

    builder.record_response(resp.status_code, dict(resp.headers), data)

    if resp.status_code == 200:
        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage", {})
        rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", "N/A")
        builder.set_parsed_output({
            "content_length": len(msg.get("content") or ""),
            "reasoning_content_length": len(msg.get("reasoning_content") or ""),
            "reasoning_tokens": rt,
            "total_tokens": usage.get("total_tokens"),
            "elapsed_seconds": round(elapsed, 2),
            "finish_reason": choice["finish_reason"],
        })
    else:
        builder.set_parsed_output({"http_error": resp.status_code, "elapsed_seconds": round(time.monotonic() - t0, 2)})
    builder.save()


async def test_f9_reasoning_effort_high(ds_config: dict) -> None:
    await _run_reffort(ds_config, "high", ArtifactBuilder("L1", "ds-native", "httpx", "F9-reasoning-effort", "high"))


async def test_f9_reasoning_effort_max(ds_config: dict) -> None:
    await _run_reffort(ds_config, "max", ArtifactBuilder("L1", "ds-native", "httpx", "F9-reasoning-effort", "max"))
