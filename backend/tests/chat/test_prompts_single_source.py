"""后置契约保护：graph.py / me.py / context.py 无 inline prompt 字面量。

M6-patch3 Step 2 已经证这三文件本来无字面量需迁移；本测试作为保护网，
防止未来修改引入 inline prompt 字面量而偏离 prompts.py 单一来源契约。
"""

import re

import pytest

PROMPT_LIKE_PATTERNS = [
    r"SystemMessage\(content=['\"](?!#|\s*$)",        # SystemMessage(content="非注释内容
    r""""""  # 多行文档字符串（仅允许函数/模块 docstring）
]

# 需要特别关注的 Python 文件
TARGET_FILES = [
    "app/chat/graph.py",
    "app/api/me.py",
    "app/chat/context.py",
]

# 已知合法的函数 docstring 行号范围（key=(basename, start), value=end）
# 扫描时跳过这些行
KNOWN_DOCSTRINGS: dict[tuple[str, int], int] = {
    # graph.py
    ("graph.py", 1): 22,
    ("graph.py", 61): 75,
    ("graph.py", 96): 100,
    ("graph.py", 110): 117,
    ("graph.py", 129): 139,
    ("graph.py", 151): 160,
    ("graph.py", 166): 174,
    ("graph.py", 190): 205,
    ("graph.py", 237): 242,
    ("graph.py", 248): 253,
    # me.py
    ("me.py", 1): 1,
    ("me.py", 57): 57,
    ("me.py", 75): 75,
    ("me.py", 97): 97,
    ("me.py", 110): 110,
    ("me.py", 164): 164,
    ("me.py", 217): 217,
    ("me.py", 292): 292,
    ("me.py", 318): 323,
    ("me.py", 376): 428,
    # context.py
    ("context.py", 1): 17,
    ("context.py", 34): 41,
    ("context.py", 71): 71,
}


def _is_in_docstring(basename: str, lineno: int) -> bool:
    """判断行号是否在已知的合法 docstring 范围内。"""
    for (fn, start), end in KNOWN_DOCSTRINGS.items():
        if fn == basename and start <= lineno <= end:
            return True
    return False


class TestNoInlinePromptLiterals:
    """graph.py / me.py / context.py 不得含有 inline prompt 字面量。"""

    @pytest.mark.parametrize("filepath", TARGET_FILES)
    def test_no_system_message_literal_content(self, filepath: str) -> None:
        """SystemMessage(content=...) 参数必须是变量引用，不得为字面量。"""
        with open(f"/app/{filepath}") as f:
            lines = f.readlines()

        basename = filepath.split("/")[-1]
        for i, line in enumerate(lines, 1):
            if _is_in_docstring(basename, i):
                continue
            stripped = line.strip()
            # 跳过注释行和 import 行
            if stripped.startswith("#") or "import " in stripped:
                continue

            # 检查 SystemMessage(content="..." ) 字面量模式
            if 'SystemMessage(content="' in stripped or "SystemMessage(content='" in stripped:
                # 排除已知的变量引用
                if "content=guidance" in stripped or "content=summary_text" in stripped:
                    continue
                if "content=COMPRESSION_PROMPT_STUB" in stripped:
                    continue
                pytest.fail(f"{filepath}:{i}: inline prompt literal detected: {stripped}")

    def test_no_suspicious_long_string(self) -> None:
        """3 文件无长字符串字面量（>100 字符）非 docstring 用途。"""
        for filepath in TARGET_FILES:
            with open(f"/app/{filepath}") as f:
                lines = f.readlines()

            basename = filepath.split("/")[-1]
            for i, line in enumerate(lines, 1):
                if _is_in_docstring(basename, i):
                    continue
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Python 字符串字面量超过 100 字符疑似 prompt
                for match in re.finditer(r'["\'](.{100,})["\']', stripped):
                    pytest.fail(
                        f"{filepath}:{i}: suspiciously long string literal "
                        f"({len(match.group(1))} chars)"
                    )
