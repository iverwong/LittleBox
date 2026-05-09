"""Tests for app.chat.extractors — provider-aware finish_reason / reasoning extraction.

M6 patch 2 (Step 11.2): covers every provider × field location combination.
"""

from langchain_core.messages import AIMessageChunk

from app.chat.extractors import (
    ALLOWED_FINISH_REASONS,
    extract_finish_reason,
    extract_reasoning_content,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunk(
    *,
    finish_reason: str | None = None,
    reasoning_content: str | None = None,
) -> AIMessageChunk:
    """Build an AIMessageChunk with the given additional_kwargs."""
    ak: dict = {}
    if finish_reason is not None:
        ak["response_metadata"] = {"finish_reason": finish_reason}
    if reasoning_content is not None:
        ak["reasoning_content"] = reasoning_content
    return AIMessageChunk(content="", additional_kwargs=ak)


# ---------------------------------------------------------------------------
# T0: ALLOWED_FINISH_REASONS frozenset
# ---------------------------------------------------------------------------


class TestAllowedFinishReasons:
    """ALLOWED_FINISH_REASONS is a frozenset with three values."""

    def test_is_frozenset(self) -> None:
        assert isinstance(ALLOWED_FINISH_REASONS, frozenset)

    def test_contains_stop(self) -> None:
        assert "stop" in ALLOWED_FINISH_REASONS

    def test_contains_length(self) -> None:
        assert "length" in ALLOWED_FINISH_REASONS

    def test_contains_content_filter(self) -> None:
        assert "content_filter" in ALLOWED_FINISH_REASONS

    def test_excludes_tool_calls(self) -> None:
        assert "tool_calls" not in ALLOWED_FINISH_REASONS


# ---------------------------------------------------------------------------
# T1: extract_finish_reason
# ---------------------------------------------------------------------------


class TestExtractFinishReason:
    """extract_finish_reason: deepseek / openai × finish_reason values."""

    # -- whitelist values --

    def test_deepseek_stop(self) -> None:
        chunk = _chunk(finish_reason="stop")
        assert extract_finish_reason(chunk, "deepseek") == "stop"

    def test_deepseek_length(self) -> None:
        chunk = _chunk(finish_reason="length")
        assert extract_finish_reason(chunk, "deepseek") == "length"

    def test_deepseek_content_filter(self) -> None:
        chunk = _chunk(finish_reason="content_filter")
        assert extract_finish_reason(chunk, "deepseek") == "content_filter"

    def test_openai_stop(self) -> None:
        chunk = _chunk(finish_reason="stop")
        assert extract_finish_reason(chunk, "openai") == "stop"

    # -- non-whitelist --

    def test_tool_calls_returns_none(self) -> None:
        chunk = _chunk(finish_reason="tool_calls")
        assert extract_finish_reason(chunk, "deepseek") is None

    def test_none_when_no_finish_reason(self) -> None:
        chunk = _chunk(finish_reason=None)
        assert extract_finish_reason(chunk, "deepseek") is None

    def test_none_when_no_response_metadata(self) -> None:
        chunk = AIMessageChunk(content="test", additional_kwargs={})
        assert extract_finish_reason(chunk, "deepseek") is None

    def test_none_when_empty_additional_kwargs(self) -> None:
        chunk = AIMessageChunk(content="test", additional_kwargs={})
        assert extract_finish_reason(chunk, "deepseek") is None

    def test_none_when_no_response_metadata_key(self) -> None:
        """additional_kwargs exists but has no response_metadata key."""
        chunk = AIMessageChunk(content="test", additional_kwargs={"some_key": "val"})
        assert extract_finish_reason(chunk, "deepseek") is None

    # -- unregistered provider (falls back to deepseek path) --

    def test_unregistered_provider_finish_reason(self) -> None:
        chunk = _chunk(finish_reason="stop")
        assert extract_finish_reason(chunk, "anthropic") == "stop"


# ---------------------------------------------------------------------------
# T2: extract_reasoning_content
# ---------------------------------------------------------------------------


class TestExtractReasoningContent:
    """extract_reasoning_content: deepseek × reasoning hit/miss; openai → None."""

    def test_deepseek_with_reasoning(self) -> None:
        chunk = _chunk(reasoning_content="思考中...")
        assert extract_reasoning_content(chunk, "deepseek") == "思考中..."

    def test_deepseek_no_reasoning(self) -> None:
        chunk = _chunk(reasoning_content=None)
        assert extract_reasoning_content(chunk, "deepseek") is None

    def test_deepseek_no_additional_kwargs(self) -> None:
        chunk = AIMessageChunk(content="test", additional_kwargs={})
        assert extract_reasoning_content(chunk, "deepseek") is None

    def test_deepseek_no_additional_kwargs_with_other_keys(self) -> None:
        """additional_kwargs exists but has no reasoning_content key."""
        chunk = AIMessageChunk(content="test", additional_kwargs={"other": "val"})
        assert extract_reasoning_content(chunk, "deepseek") is None

    def test_openai_always_none(self) -> None:
        chunk = _chunk(reasoning_content="思考中...")
        assert extract_reasoning_content(chunk, "openai") is None

    def test_unregistered_provider_returns_none(self) -> None:
        chunk = _chunk(reasoning_content="思考中...")
        assert extract_reasoning_content(chunk, "anthropic") is None
