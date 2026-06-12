"""compression.py 单元测试：阈值常量 + build_compression_prompt + extract_compression_summary。"""
from __future__ import annotations

from app.domain.chat.compression import (
    CONTEXT_COMPRESS_THRESHOLD_TOKENS,
    build_compression_prompt,
    extract_compression_summary,
)
from app.domain.chat.prompts import COMPRESSION_PROMPT_STUB
from app.core.llm_extractors import extract_usage
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage


def _make_chunk(usage: dict | None = None) -> AIMessageChunk:
    """构造一个携带 usage_metadata 的 AIMessageChunk。"""
    return AIMessageChunk(content="", usage_metadata=usage)


class TestExtractUsage:
    """extract_usage 边界覆盖。"""

    def test_usage_none(self):
        assert extract_usage(_make_chunk(None)) is None

    def test_usage_zero(self):
        um = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        got = extract_usage(_make_chunk(um))
        assert got == um

    def test_usage_typical(self):
        um = {"input_tokens": 350, "output_tokens": 120, "total_tokens": 470}
        got = extract_usage(_make_chunk(um))
        assert got == um

    def test_usage_large(self):
        um = {"input_tokens": 300_000, "output_tokens": 200_001, "total_tokens": 500_001}
        got = extract_usage(_make_chunk(um))
        assert got == um


class TestCompressionPrompt:
    """build_compression_prompt 新结构契约。"""

    def test_always_two_messages(self):
        """返回 list 长度恒为 2。"""
        msgs = [AIMessageChunk(content="你好"), AIMessageChunk(content="世界")]
        result = build_compression_prompt(msgs)
        assert len(result) == 2

    def test_empty_history(self):
        """空 history 仍返回 2 条消息。"""
        result = build_compression_prompt([])
        assert len(result) == 2

    def test_first_is_system(self):
        result = build_compression_prompt([])
        assert result[0].type == "system"
        assert "对话压缩助手" in result[0].content

    def test_second_is_human(self):
        result = build_compression_prompt([])
        assert result[1].type == "human"

    def test_system_contains_stub(self):
        """SystemMessage content 包含 COMPRESSION_PROMPT_STUB（任务 + 输出契约）。"""
        result = build_compression_prompt([])
        assert COMPRESSION_PROMPT_STUB in result[0].content

    def test_human_contains_history_xml(self):
        """HumanMessage content 包含 <history> 序列化。"""
        result = build_compression_prompt([
            HumanMessage(content="你好"),
            AIMessage(content="嗨"),
        ])
        assert "<history>" in result[1].content
        assert '<turn idx="1" role="user">你好</turn>' in result[1].content
        assert '<turn idx="1" role="assistant">嗨</turn>' in result[1].content
        assert "</history>" in result[1].content

    def test_system_contains_output_contract(self):
        """SystemMessage content 包含 <summary> 输出契约（与任务一同收口到 STUB）。"""
        result = build_compression_prompt([])
        assert "<summary>" in result[0].content

    def test_last_turn_ai_not_leaked_to_system(self):
        """末条为 AIMessage 时，它嵌入在 <history> 中而非裸露在 SystemMessage 后。"""
        history = [
            HumanMessage(content="问题"),
            AIMessage(content="回复"),
        ]
        result = build_compression_prompt(history)
        human_content = result[1].content
        # 末条 AI 回复应出现在 <history> 内
        assert '<turn idx="1" role="assistant">回复</turn>' in human_content
        # 不应出现裸露的 AIMessage（非 XML 格式）
        assert "回复" in human_content


class TestExtractCompressionSummary:
    """extract_compression_summary 各种场景。"""

    def test_normal(self):
        result = extract_compression_summary("<summary>这是摘要</summary>")
        assert result == "这是摘要"

    def test_with_surrounding_text(self):
        """容忍 tag 前后的其他文字。"""
        result = extract_compression_summary(
            "好的，以下是摘要：\n<summary>这是摘要内容</summary>\n-- end"
        )
        assert result == "这是摘要内容"

    def test_tag_missing_fallback(self):
        """tag 缺失时兜底返回 raw_output.strip()。"""
        result = extract_compression_summary("  纯文本回复  ")
        assert result == "纯文本回复"

    def test_empty_string_fallback(self):
        result = extract_compression_summary("")
        assert result == ""

    def test_multiple_tags_takes_first(self):
        result = extract_compression_summary(
            "<summary>第一个</summary><summary>第二个</summary>"
        )
        assert result == "第一个"


class TestThresholdConstant:
    def test_threshold(self):
        assert CONTEXT_COMPRESS_THRESHOLD_TOKENS == 500_000
