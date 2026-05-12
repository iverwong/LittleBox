"""compression.py 单元测试：estimate_tokens 边界 + 阈值常量。"""

import pytest

from app.chat.compression import CONTEXT_COMPRESS_THRESHOLD_TOKENS, estimate_tokens


class TestEstimateTokens:
    """estimate_tokens 边界覆盖。"""

    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_ascii_one_char(self):
        assert estimate_tokens("a") == 1

    def test_ascii_four_chars(self):
        """4 ASCII = 1 token。"""
        assert estimate_tokens("abcd") == 1

    def test_ascii_five_chars(self):
        """5 ASCII → ceil(5/4) = 2。"""
        assert estimate_tokens("abcde") == 2

    def test_cjk_only(self):
        """每个中文 = 1 token。"""
        assert estimate_tokens("你好世界") == 4

    def test_mixed_cjk_ascii(self):
        """混合：中文逐字 + ASCII 每 4 字 1 token。"""
        assert estimate_tokens("你好abc世界") == 4 + 1

    def test_threshold_constant(self):
        assert CONTEXT_COMPRESS_THRESHOLD_TOKENS == 500_000
