"""compression.py 单元测试：阈值常量 + build_compression_prompt + extract_compression_summary。"""
from __future__ import annotations

from types import SimpleNamespace

from app.domain.chat.compression import (
    COMPRESSION_KEEP_RECENT_PAIRS,
    CONTEXT_COMPRESS_THRESHOLD_TOKENS,
    build_compression_messages,
    extract_compression_summary,
    split_for_compression,
)
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage


class TestCompressionPrompt:
    """build_compression_prompt 新结构契约。"""

    def test_always_two_messages(self):
        """返回 list 长度恒为 2。"""
        msgs = [AIMessageChunk(content="你好"), AIMessageChunk(content="世界")]
        result = build_compression_messages(None, msgs)
        assert len(result) == 2

    def test_empty_history(self):
        """空 history 仍返回 2 条消息。"""
        result = build_compression_messages(None, [])
        assert len(result) == 2

    def test_first_is_system(self):
        result = build_compression_messages(None, [])
        assert result[0].type == "system"
        assert "对话压缩助手" in result[0].content

    def test_second_is_human(self):
        result = build_compression_messages(None, [])
        assert result[1].type == "human"

    def test_system_contains_stub(self):
        """SystemMessage content 包含 COMPRESSION_PROMPT_STUB 关键句（任务 + 输出契约）。"""
        result = build_compression_messages(None, [])
        # 当前实现 prompt 多行版,逐句断言关键短语
        assert "你是对话压缩助手" in result[0].content
        assert "你需要使用第三人称" in result[0].content
        assert "<history>" in result[0].content
        assert "<summary>" in result[0].content

    def test_human_contains_history_xml(self):
        """HumanMessage content 包含 <history> 序列化。"""
        result = build_compression_messages(None, [
            HumanMessage(content="你好"),
            AIMessage(content="嗨"),
        ])
        assert "<history>" in result[1].content
        assert '<turn idx="1" role="user">你好</turn>' in result[1].content
        assert '<turn idx="1" role="assistant">嗨</turn>' in result[1].content
        assert "</history>" in result[1].content

    def test_system_contains_output_contract(self):
        """SystemMessage content 包含 <summary> 输出契约（与任务一同收口到 STUB）。"""
        result = build_compression_messages(None, [])
        assert "<summary>" in result[0].content

    def test_last_turn_ai_not_leaked_to_system(self):
        """末条为 AIMessage 时，它嵌入在 <history> 中而非裸露在 SystemMessage 后。"""
        history = [
            HumanMessage(content="问题"),
            AIMessage(content="回复"),
        ]
        result = build_compression_messages(None, history)
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


def _mk(content: str) -> SimpleNamespace:
    """构造一个仅含 content 字段的伪 Message,用于切分纯函数单测。"""
    return SimpleNamespace(content=content)


class TestSplitForCompression:
    """split_for_compression — 切分边界 + 顺序契约。"""

    def test_keep_recent_pairs_default_is_three(self):
        """默认 N=3,常量导出值即函数默认。"""
        assert COMPRESSION_KEEP_RECENT_PAIRS == 3

    def test_more_than_keep_n_splits_oldest_to_compress(self):
        """10 条 (>3 对): 前 4 条进 to_compress,后 6 条进 to_keep。"""
        actives = [_mk(f"m{i}") for i in range(10)]
        to_compress, to_keep = split_for_compression(actives)
        assert [m.content for m in to_compress] == ["m0", "m1", "m2", "m3"]
        assert [m.content for m in to_keep] == ["m4", "m5", "m6", "m7", "m8", "m9"]

    def test_exactly_keep_n_all_to_compress_none_to_keep(self):
        """6 条 (=3 对): 全部进 to_compress,to_keep 为空(已超阈值,不留原会话)。"""
        actives = [_mk(f"m{i}") for i in range(6)]
        to_compress, to_keep = split_for_compression(actives)
        assert [m.content for m in to_compress] == ["m0", "m1", "m2", "m3", "m4", "m5"]
        assert to_keep == []

    def test_fewer_than_keep_n_all_to_compress(self):
        """3 条 (<3 对): 全部进 to_compress,to_keep 为空。"""
        actives = [_mk(f"m{i}") for i in range(3)]
        to_compress, to_keep = split_for_compression(actives)
        assert [m.content for m in to_compress] == ["m0", "m1", "m2"]
        assert to_keep == []

    def test_empty_input_both_empty(self):
        """空输入: 两侧都空(to_compress 空,to_keep 也空,不会调 LLM)。"""
        to_compress, to_keep = split_for_compression([])
        assert to_compress == []
        assert to_keep == []

    def test_just_above_keep_n_boundary(self):
        """7 条 (>3 对边界): 1 条进 to_compress,6 条进 to_keep。"""
        actives = [_mk(f"m{i}") for i in range(7)]
        to_compress, to_keep = split_for_compression(actives)
        assert [m.content for m in to_compress] == ["m0"]
        assert [m.content for m in to_keep] == ["m1", "m2", "m3", "m4", "m5", "m6"]

    def test_custom_keep_n(self):
        """keep_recent_pairs=1: 4 条 → 2 条进 to_compress,2 条进 to_keep。"""
        actives = [_mk(f"m{i}") for i in range(4)]
        to_compress, to_keep = split_for_compression(actives, keep_recent_pairs=1)
        assert [m.content for m in to_compress] == ["m0", "m1"]
        assert [m.content for m in to_keep] == ["m2", "m3"]

    def test_returns_new_lists_not_aliases(self):
        """返回值是新建 list,跟输入不共享引用(防调用方原地修改污染)。"""
        actives = [_mk(f"m{i}") for i in range(8)]
        to_compress, to_keep = split_for_compression(actives)
        assert to_compress is not actives
        assert to_keep is not actives
        assert to_compress is not to_keep

