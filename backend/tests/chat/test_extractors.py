"""Tests for app.core.llm_extractors — ModelProfile 解耦的提取器。

Step 5 重构后契约：
- `extract_finish_reason(chunk)`：无 provider 入参,白名单透传路径与 provider 无关。
- `extract_reasoning_content(chunk, profile)`：由 `ModelProfile.supports_reasoning` 决定
  是否走 `additional_kwargs["reasoning_content"]` 提取路径,非推理档返回 None。
- `role_profile(role)`：role → ModelProfile 解析入口,统一由本测试构造 profile 喂入。
"""

from app.core.llm_extractors import (
    ALLOWED_FINISH_REASONS,
    extract_finish_reason,
    extract_reasoning_content,
    role_profile,
)
from app.core.llm_topology import ModelProfile, Role, Transport
from langchain_core.messages import AIMessageChunk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunk(
    *,
    finish_reason: str | None = None,
    reasoning_content: str | None = None,
) -> AIMessageChunk:
    """Build an AIMessageChunk with the given finish_reason / reasoning_content.

    M8-hotfix: finish_reason 通过 response_metadata 直接属性传入（真路径），
    不再走 additional_kwargs["response_metadata"] 错误路径。
    """
    kwargs: dict = {}
    if finish_reason is not None:
        kwargs["response_metadata"] = {"finish_reason": finish_reason}
    if reasoning_content is not None:
        kwargs["additional_kwargs"] = {"reasoning_content": reasoning_content}
    return AIMessageChunk(content="", **kwargs)


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
    """extract_finish_reason: 白名单透传 + 非白名单 None 路径。"""

    # -- 白名单值透传（Step 5 去 provider 形参后字节级统一,deepseek/openai
    #    旧分支同构合并,不再分 provider 双写） --

    def test_stop(self) -> None:
        chunk = _chunk(finish_reason="stop")
        assert extract_finish_reason(chunk) == "stop"

    def test_length(self) -> None:
        chunk = _chunk(finish_reason="length")
        assert extract_finish_reason(chunk) == "length"

    def test_content_filter(self) -> None:
        chunk = _chunk(finish_reason="content_filter")
        assert extract_finish_reason(chunk) == "content_filter"

    # -- 非白名单 --

    def test_tool_calls_returns_none(self) -> None:
        chunk = _chunk(finish_reason="tool_calls")
        assert extract_finish_reason(chunk) is None

    # -- 缺字段路径（Step 5 合并两个同构用例为单测,保留更直观的命名） --

    def test_none_when_no_finish_reason(self) -> None:
        chunk = _chunk(finish_reason=None)
        assert extract_finish_reason(chunk) is None

    def test_none_when_additional_kwargs_empty(self) -> None:
        """Step 5 合并:原 test_none_when_no_response_metadata +
        test_none_when_empty_additional_kwargs 字节同构
        （均 AIMessageChunk(content="test", additional_kwargs={})）,合并保留。
        """
        chunk = AIMessageChunk(content="test", additional_kwargs={})
        assert extract_finish_reason(chunk) is None

    def test_none_when_additional_kwargs_lacks_response_metadata(self) -> None:
        """additional_kwargs 存在但不含 response_metadata 键。"""
        chunk = AIMessageChunk(content="test", additional_kwargs={"some_key": "val"})
        assert extract_finish_reason(chunk) is None


# ---------------------------------------------------------------------------
# T2: extract_reasoning_content
# ---------------------------------------------------------------------------


class TestExtractReasoningContent:
    """extract_reasoning_content: 由 ModelProfile.supports_reasoning 判定是否提取。"""

    # -- 推理档（deepseek-v4）真分支：role_profile(Role.MAIN) 解析得到
    #    ModelProfile("deepseek-v4", supports_reasoning=True) --

    def test_supports_reasoning_true_with_reasoning(self) -> None:
        chunk = _chunk(reasoning_content="思考中...")
        assert extract_reasoning_content(chunk, role_profile(Role.MAIN)) == "思考中..."

    def test_supports_reasoning_true_no_reasoning(self) -> None:
        chunk = _chunk(reasoning_content=None)
        assert extract_reasoning_content(chunk, role_profile(Role.MAIN)) is None

    def test_supports_reasoning_true_no_additional_kwargs(self) -> None:
        chunk = AIMessageChunk(content="test", additional_kwargs={})
        assert extract_reasoning_content(chunk, role_profile(Role.MAIN)) is None

    def test_supports_reasoning_true_additional_kwargs_lacks_reasoning(self) -> None:
        """additional_kwargs 存在但不含 reasoning_content 键。"""
        chunk = AIMessageChunk(content="test", additional_kwargs={"other": "val"})
        assert extract_reasoning_content(chunk, role_profile(Role.MAIN)) is None

    # -- 非推理档（Step 5 新增真分支,Step 0 计划清单要求覆盖 supports_reasoning=False） --

    def test_supports_reasoning_false_returns_none(self) -> None:
        """ModelProfile.supports_reasoning=False 档位（不论字段是否携带 reasoning_content）,
        一律返回 None。Step 5 新增真分支,旧 test_unregistered_provider_returns_none 与
        test_openai_always_none 合并至此。
        """
        fake_profile = ModelProfile(
            family="fake-no-reasoning",
            transport=Transport.CHAT_DEEPSEEK,
            supports_reasoning=False,
            supports_tools=False,
            multimodal=False,
        )
        # 字段携带 reasoning_content 也应返回 None（档位优先于字段存在）
        chunk_with = _chunk(reasoning_content="思考中...")
        assert extract_reasoning_content(chunk_with, fake_profile) is None
        # 字段不携带更应返回 None
        chunk_without = _chunk(reasoning_content=None)
        assert extract_reasoning_content(chunk_without, fake_profile) is None
        # additional_kwargs 缺失也返回 None
        chunk_empty = AIMessageChunk(content="test", additional_kwargs={})
        assert extract_reasoning_content(chunk_empty, fake_profile) is None
