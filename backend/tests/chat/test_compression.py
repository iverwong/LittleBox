"""compression.py 单元测试：extract_usage + 阈值常量 + build_compression_prompt。"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, AIMessage, SystemMessage

from app.chat.compression import (
    COMPRESSION_PROMPT_STUB,
    CONTEXT_COMPRESS_THRESHOLD_TOKENS,
    build_compression_prompt,
)
from app.chat.extractors import extract_usage
from app.chat.prompts import SUMMARY_PREFIX


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
    """build_compression_prompt 基础契约。"""

    def test_prompt_structure(self):
        msgs = [AIMessageChunk(content="你好"), AIMessageChunk(content="世界")]
        result = build_compression_prompt(msgs)
        assert len(result) == 3
        assert result[0].content == COMPRESSION_PROMPT_STUB

    def test_empty_history(self):
        result = build_compression_prompt([])
        assert len(result) == 1
        assert result[0].content == COMPRESSION_PROMPT_STUB


class TestThresholdConstant:
    def test_threshold(self):
        assert CONTEXT_COMPRESS_THRESHOLD_TOKENS == 500_000


class TestSecondaryCompression:
    """二次压缩：旧的 summary 行应包含在压缩集中。"""

    def test_old_summary_included_in_prompt(self):
        """build_compression_prompt 传入含 summary 的 actives → summary 作为 SystemMessage 保留在输入中。"""
        lc_msgs = [
            HumanMessage(content="你好"),
            AIMessage(content="今天过得怎么样？"),
            SystemMessage(content=SUMMARY_PREFIX + "上次讨论：数学作业"),
        ]
        result = build_compression_prompt(lc_msgs)
        assert len(result) == 4
        assert result[3].content == SUMMARY_PREFIX + "上次讨论：数学作业"

    def test_old_summary_middle_position(self):
        """summary 行在中间时，build_compression_prompt 保持原始顺序。"""
        lc_msgs = [
            HumanMessage(content="第一轮"),
            AIMessage(content="回复"),
            SystemMessage(content=SUMMARY_PREFIX + "旧摘要"),
            HumanMessage(content="第二轮"),
            AIMessage(content="第二轮回复"),
        ]
        result = build_compression_prompt(lc_msgs)
        assert len(result) == 6
        # 原始顺序：[stub, human, ai, summary, human, ai]
        assert isinstance(result[1], HumanMessage)
        assert isinstance(result[3], SystemMessage)
        assert result[3].content == SUMMARY_PREFIX + "旧摘要"
