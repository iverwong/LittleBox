"""history_xml.py 单元测试：XML 序列化与输出提取。"""
from __future__ import annotations

from app.core.history_xml import (
    escape_xml_text,
    extract_wrapped_output,
    serialize_history_to_xml,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


class TestEscapeXmlText:
    """escape_xml_text 基础转义。"""

    def test_no_special_chars(self):
        assert escape_xml_text("纯文本 abc 123") == "纯文本 abc 123"

    def test_ampersand(self):
        assert escape_xml_text("A & B") == "A &amp; B"

    def test_less_than(self):
        assert escape_xml_text("a < b") == "a &lt; b"

    def test_greater_than(self):
        assert escape_xml_text("a > b") == "a &gt; b"

    def test_all_special(self):
        assert escape_xml_text("<hello & world>") == "&lt;hello &amp; world&gt;"

    def test_no_double_escape(self):
        """确保已转义的实体不会被二次转义。"""
        assert escape_xml_text("&amp;") == "&amp;amp;"


class TestSerializeHistoryEmpty:
    """空 history。"""

    def test_empty_returns_empty_xml(self):
        result = serialize_history_to_xml([])
        assert result == "<history></history>"

    def test_only_system_default_skipped(self):
        result = serialize_history_to_xml([SystemMessage(content="sys")])
        assert result == "<history></history>"


class TestSerializeHistorySingleTurn:
    """单条 user 消息。"""

    def test_single_user(self):
        result = serialize_history_to_xml([HumanMessage(content="你好")])
        assert result == '<history><turn idx="1" role="user">你好</turn></history>'

    def test_user_without_assistant(self):
        """末尾孤立 user。"""
        result = serialize_history_to_xml([HumanMessage(content="hello")])
        assert result == '<history><turn idx="1" role="user">hello</turn></history>'


class TestSerializeHistoryPair:
    """user + assistant 配对。"""

    def test_user_assistant_share_idx(self):
        result = serialize_history_to_xml([
            HumanMessage(content="你好"),
            AIMessage(content="你好呀"),
        ])
        assert (
            result
            == '<history><turn idx="1" role="user">你好</turn><turn idx="1" role="assistant">你好呀</turn></history>'
        )


class TestSerializeHistoryTwoRounds:
    """两轮完整对话。"""

    def test_two_rounds(self):
        result = serialize_history_to_xml([
            HumanMessage(content="第一轮"),
            AIMessage(content="回复一"),
            HumanMessage(content="第二轮"),
            AIMessage(content="回复二"),
        ])
        assert (
            result
            == '<history><turn idx="1" role="user">第一轮</turn><turn idx="1" role="assistant">回复一</turn><turn idx="2" role="user">第二轮</turn><turn idx="2" role="assistant">回复二</turn></history>'
        )


class TestSerializeHistoryOrphanUser:
    """末尾孤立 user 无 assistant 对应。"""

    def test_orphan_user(self):
        result = serialize_history_to_xml([
            HumanMessage(content="第一轮"),
            AIMessage(content="回复"),
            HumanMessage(content="第二轮（未回复）"),
        ])
        assert (
            result
            == '<history><turn idx="1" role="user">第一轮</turn><turn idx="1" role="assistant">回复</turn><turn idx="2" role="user">第二轮（未回复）</turn></history>'
        )


class TestSerializeHistoryConsecutiveAi:
    """连续 AIMessage 复用同一 idx。"""

    def test_consecutive_ai(self):
        result = serialize_history_to_xml([
            HumanMessage(content="问题"),
            AIMessage(content="回复一"),
            AIMessage(content="补充回复"),
        ])
        # 两条 AI 共享 idx="1"
        parts = [
            '<turn idx="1" role="user">问题</turn>',
            '<turn idx="1" role="assistant">回复一</turn>',
            '<turn idx="1" role="assistant">补充回复</turn>',
        ]
        assert result == f"<history>{''.join(parts)}</history>"

    def test_ai_only_history(self):
        """防御：以 AIMessage 开头的 history（不应发生但兜底）。"""
        result = serialize_history_to_xml([
            AIMessage(content="孤立助手消息"),
        ])
        # 兜底 current_idx = 1
        assert result == '<history><turn idx="1" role="assistant">孤立助手消息</turn></history>'


class TestSerializeHistoryEscaping:
    """XML 特殊字符转义。"""

    def test_less_than(self):
        result = serialize_history_to_xml([HumanMessage(content="x < 3")])
        assert "&lt;" in result
        # content 中的 < 已被转义为 &lt;，不会出现额外的 XML 标签
        assert "&lt; 3" in result

    def test_greater_than(self):
        result = serialize_history_to_xml([HumanMessage(content="x > 3")])
        assert "&gt;" in result

    def test_ampersand(self):
        result = serialize_history_to_xml([HumanMessage(content="A & B")])
        assert "&amp;" in result

    def test_close_turn_literal(self):
        """模拟用户输入中包含 </turn> 字面量。"""
        result = serialize_history_to_xml([HumanMessage(content="</turn>")])
        assert "&lt;/turn&gt;" in result
        # 不应出现额外的 </turn> 闭合标签
        assert result.count("</turn>") == 1  # 仅序列化构造的闭合标签


class TestSerializeHistorySystem:
    """SystemMessage 处理。"""

    def test_system_skipped_by_default(self):
        result = serialize_history_to_xml([
            SystemMessage(content="你是助手"),
            HumanMessage(content="你好"),
            AIMessage(content="嗨"),
        ])
        # SystemMessage 被跳过
        assert '<turn idx="sys"' not in result
        assert '<turn idx="1" role="user">你好</turn>' in result

    def test_system_included(self):
        result = serialize_history_to_xml([
            SystemMessage(content="你是助手"),
            HumanMessage(content="你好"),
        ], include_system=True)
        assert '<turn idx="sys" role="system">你是助手</turn>' in result
        assert '<turn idx="1" role="user">你好</turn>' in result


class TestExtractWrappedOutput:
    """extract_wrapped_output 各种场景。"""

    def test_normal(self):
        result = extract_wrapped_output("<summary>这是摘要内容</summary>", "summary")
        assert result == "这是摘要内容"

    def test_with_whitespace(self):
        result = extract_wrapped_output(
            "  <summary>  带空白的内容  </summary>  ", "summary"
        )
        assert result == "带空白的内容"

    def test_with_code_fence(self):
        raw = '```xml\n<summary>摘要在 fence 中</summary>\n```'
        result = extract_wrapped_output(raw, "summary")
        assert result == "摘要在 fence 中"

    def test_multiple_occurrences_takes_first(self):
        raw = "<summary>第一个</summary><summary>第二个</summary>"
        result = extract_wrapped_output(raw, "summary")
        assert result == "第一个"

    def test_not_found_returns_none(self):
        result = extract_wrapped_output("没有任何标签的内容", "summary")
        assert result is None

    def test_different_tag_name(self):
        raw = "<reply>回复内容</reply>"
        result = extract_wrapped_output(raw, "reply")
        assert result == "回复内容"

    def test_nested_tags(self):
        """嵌套 tag，应只取最外层的匹配。"""
        raw = "<outer><inner>deep</inner></outer>"
        result = extract_wrapped_output(raw, "outer")
        assert result == "<inner>deep</inner>"
