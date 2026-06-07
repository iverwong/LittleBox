"""补3: factory.py max_retries 覆写检查。

检查要点:
  - _build_chat_deepseek / _build_chat_openai 是否给底层 openai client 传了 max_retries
  - max_retries=0 vs 默认(=2) 在 F6 mock fallback 下主端 HTTP 调用次数
  - 生产构造是否应固定 max_retries=0
"""
from __future__ import annotations

import httpx
import pytest
import respx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek

from .conftest import ArtifactBuilder, SYSTEM_MESSAGE, USER_MESSAGE

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_MOCK_200 = {
    "id": "mock_cmpl", "object": "chat.completion", "created": 1700000000,
    "model": "deepseek-v4-flash",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "mock ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

_MESSAGES = [SystemMessage(content=SYSTEM_MESSAGE), HumanMessage(content=USER_MESSAGE)]


async def _run_fallback_test(
    cfg: dict,
    max_retries: int,
    builder: ArtifactBuilder,
) -> int:
    """创建一个 fallback chain，主端 mock httpx.ConnectError，备端 mock 200，返回主端请求次数。"""
    primary_url = f"{cfg['base_url']}/chat/completions"
    backup_url = "https://mock-backup.example.com/v1/chat/completions"

    async with respx.mock(assert_all_mocked=True) as mock:
        mock.post(primary_url).mock(side_effect=httpx.ConnectError("mock primary fail"))
        mock.post(backup_url).respond(status_code=200, json=_MOCK_200)

        raw = builder.make_http_client()
        primary = ChatDeepSeek(
            api_key=cfg["api_key"],
            api_base=cfg["base_url"],
            model=cfg["model"],
            http_async_client=raw,
            timeout=10,
            max_retries=max_retries,
            extra_body={"thinking": {"type": "enabled"}},
        )
        backup = ChatDeepSeek(
            api_key=cfg["api_key"],
            api_base="https://mock-backup.example.com/v1",
            model=cfg["model"],
            http_async_client=raw,
            timeout=10,
            max_retries=0,
            extra_body={"thinking": {"type": "enabled"}},
        )
        chain = primary.with_fallbacks([backup])
        try:
            response = await chain.ainvoke(_MESSAGES)
            _ = response.content
        except Exception:
            pass

    # 统计主端请求次数
    primary_calls = [e for e in builder.req_entries if primary_url in e["url"]]
    return len(primary_calls)


async def test_factory_check_max_retries_code(ds_config: dict) -> None:
    """检查 factory.py 是否显式传了 max_retries。"""
    src = open("/app/app/core/llm.py").read()
    has_max_retries = "max_retries" in src
    print(f"\n=== factory.py max_retries 检查 ===")
    print(f"max_retries 出现: {has_max_retries}")
    for i, line in enumerate(src.split("\n"), 1):
        if "max_retries" in line or "ChatDeepSeek(" in line or "ChatOpenAI(" in line:
            print(f"  L{i}: {line.strip()}")


async def test_f6_maxretries_default(bl_config: dict) -> None:
    """默认 max_retries（=2）→ 主端应被重试多次。"""
    # 用 bl_config（DS 端 no-required 兜底走 auto 也行，但 mock 不需要真通）
    builder = ArtifactBuilder("L3", "maxretry-test", "ChatDeepSeek", "F6-maxretries-default", "enabled")
    call_count = await _run_fallback_test(bl_config, max_retries=2, builder=builder)
    builder.set_parsed_output({
        "max_retries_setting": 2,
        "primary_call_count": call_count,
        "note": f"max_retries=2 时，主端被调用 {call_count} 次（1 原始 + 重试）",
    })
    builder.save()
    print(f"\nmax_retries=2 → primary 被调用 {call_count} 次")


async def test_f6_maxretries_zero(bl_config: dict) -> None:
    """max_retries=0 → 主端应只被调用 1 次。"""
    builder = ArtifactBuilder("L3", "maxretry-test", "ChatDeepSeek", "F6-maxretries-zero", "enabled")
    call_count = await _run_fallback_test(bl_config, max_retries=0, builder=builder)
    builder.set_parsed_output({
        "max_retries_setting": 0,
        "primary_call_count": call_count,
        "note": f"max_retries=0 时，主端被调用 {call_count} 次",
    })
    builder.save()
    print(f"\nmax_retries=0 → primary 被调用 {call_count} 次")
    assert call_count == 1, f"max_retries=0 时主端应只调 1 次，实际 {call_count}"
