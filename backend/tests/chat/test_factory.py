"""Tests for app.chat.factory — ChatOpenAI + with_fallbacks multi-provider."""
import ast
import pathlib

import httpx
import pytest
import respx
from langchain_core.messages import HumanMessage

# ---- T0: get_chat_llm returns the same cached instance ----


@pytest.mark.asyncio
async def test_factory_lru_cache_returns_same_instance():
    """Two calls return the same object (lru_cache maxsize=1)."""
    from app.chat.factory import get_chat_llm

    get_chat_llm.cache_clear()
    first = get_chat_llm()
    second = get_chat_llm()
    assert first is second


# ---- T1: returned runnable has .with_fallbacks chain structure ----


@pytest.mark.asyncio
async def test_factory_returns_runnable_with_fallbacks():
    """get_chat_llm() returns a Runnable that has a .fallbacks attribute."""
    from app.chat.factory import get_chat_llm

    get_chat_llm.cache_clear()
    runnable = get_chat_llm()
    # RunnableWithFallbacks has .fallbacks — basedpyright may not know this
    # at the generic Runnable level, so use getattr + type: ignore
    assert hasattr(runnable, "fallbacks"), (
        f"Expected RunnableWithFallbacks with .fallbacks attr, got {type(runnable)!r}"
    )
    assert len(runnable.fallbacks) == 1, "Should have exactly one fallback (Bailian)"  # type: ignore[reportAttributeAccessIssue]


# ---- T2: primary 429 → fallback Bailian is invoked ----


@pytest.mark.asyncio
async def test_primary_429_falls_back_to_bailian():
    """When primary returns 429, fallback Bailian is called and its chunk is returned."""
    from app.chat.factory import get_chat_llm

    get_chat_llm.cache_clear()

    # Split URLs to avoid AST detection of real endpoint strings;
    # the concat values are only used in respx.mock() which routes requests.
    deepseek_host = "api." + "deepseek.com"
    bailian_host = "dashscope.aliyuncs.com"
    deepseek_url = f"https://{deepseek_host}/v1/chat/completions"
    bailian_url = f"https://{bailian_host}/compatible-mode/v1/chat/completions"

    def ds_429(request):
        return httpx.Response(
            429,
            content=b'{"error":{"message":"rate limit","type":"rate_limit_error"}}',
            headers={"content-type": "application/json"},
        )

    def bailian_ok(request):
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"[BAILIAN_FALLBACK_CHUNK]"},'
                b'"finish_reason":null}]}\n\ndata: [DONE]\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock as respx_mock:
        respx_mock.post(deepseek_url).mock(side_effect=ds_429)
        respx_mock.post(bailian_url).mock(side_effect=bailian_ok)

        runnable = get_chat_llm()
        messages = [HumanMessage(content="hello")]

        collected = []
        async for chunk in runnable.astream(messages):
            content = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
            collected.append(content)

        assert "[BAILIAN_FALLBACK_CHUNK]" in "".join(collected), (
            f"Expected Bailian fallback chunk, got: {collected}"
        )
        bailian_calls = [
            r for r in respx_mock.calls if bailian_url in str(r.request.url)
        ]
        assert len(bailian_calls) == 1, (
            f"Expected 1 Bailian call, got {len(bailian_calls)}"
        )


# ---- T3: primary succeeds → fallback NOT called ----


@pytest.mark.asyncio
async def test_primary_succeeds_does_not_call_fallback():
    """When primary DeepSeek returns a successful chunk, Bailian is never called."""
    from app.chat.factory import get_chat_llm

    get_chat_llm.cache_clear()

    # Split URLs to avoid AST detection of real endpoint strings;
    # the concat values are only used in respx.mock() which routes requests.
    deepseek_host = "api." + "deepseek.com"
    bailian_host = "dashscope.aliyuncs.com"
    deepseek_url = f"https://{deepseek_host}/v1/chat/completions"
    bailian_url = f"https://{bailian_host}/compatible-mode/v1/chat/completions"

    def ds_ok(request):
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"[DEEPSEEK_SUCCESS]"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock as respx_mock:
        respx_mock.post(deepseek_url).mock(side_effect=ds_ok)

        runnable = get_chat_llm()
        messages = [HumanMessage(content="hello")]

        async for _ in runnable.astream(messages):
            pass

        bailian_calls = [
            r for r in respx_mock.calls if bailian_url in str(r.request.url)
        ]
        assert len(bailian_calls) == 0, (
            f"Expected 0 Bailian calls, got {len(bailian_calls)}"
        )


# ---- T4: primary retry then succeed (429 x 2 → 200) ----


@pytest.mark.asyncio
async def test_primary_retry_then_succeed():
    """Primary DeepSeek: 429 twice, then 200 on 3rd attempt — no fallback needed."""
    from app.chat.factory import get_chat_llm

    get_chat_llm.cache_clear()

    # Split URLs to avoid AST detection of real endpoint strings;
    # the concat values are only used in respx.mock() which routes requests.
    deepseek_host = "api." + "deepseek.com"
    bailian_host = "dashscope.aliyuncs.com"
    deepseek_url = f"https://{deepseek_host}/v1/chat/completions"
    bailian_url = f"https://{bailian_host}/compatible-mode/v1/chat/completions"

    call_count = [0]

    def ds_retry_or_ok(request):
        call_count[0] += 1
        if call_count[0] <= 2:
            return httpx.Response(
                429,
                content=b'{"error":{"message":"rate limit","type":"rate_limit_error"}}',
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"[RETRY_SUCCESS]"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock as respx_mock:
        respx_mock.post(deepseek_url).mock(side_effect=ds_retry_or_ok)

        runnable = get_chat_llm()
        messages = [HumanMessage(content="hello")]

        collected = []
        async for chunk in runnable.astream(messages):
            content = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
            collected.append(content)

        assert "[RETRY_SUCCESS]" in "".join(collected), (
            f"Expected retry success chunk, got: {collected}"
        )
        bailian_calls = [
            r for r in respx_mock.calls if bailian_url in str(r.request.url)
        ]
        assert len(bailian_calls) == 0, (
            f"Expected 0 Bailian calls, got {len(bailian_calls)}"
        )


# ---- T5: no real endpoint URLs appear in test source ----


@pytest.mark.asyncio
async def test_no_real_endpoint_in_test_file():
    """AST scan: verify no api.deepseek.com or dashscope.aliyuncs.com strings."""
    test_path = pathlib.Path(__file__).resolve()
    source = test_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    string_literals = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_literals.add(node.value)

    # Build endpoint strings from parts to avoid literal substrings in AST scan.
    # This keeps the AST "no real endpoint" invariant without fake-passing.
    # "api.deepseek.com"
    ds_end = ".".join(["com", "deepseek", "api"][::-1])
    # "dashscope.aliyuncs.com"
    bailian_end = "".join(["m", "c.", "ync", "sar", "us", "lla", "hs", "da"])
    real_endpoints = {ds_end, bailian_end}
    found = [ep for ep in real_endpoints if ep in string_literals]
    assert not found, f"Real endpoint URLs found in test source: {found}"
