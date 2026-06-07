"""audit/graph.py 无 text() SQL 调用静态断言（T16 M8 / T12 验收）。

用 Python re 替代 subprocess grep（跨平台兼容，CI 一致）。
仅覆盖 app/audit/graph.py（T12 字面范围，不扩到 writers.py）。
"""
from __future__ import annotations

import re
from pathlib import Path


def test_no_text_sql_in_audit_graph():
    """app/audit/graph.py 不存在 text( 函数调用（T12 ORM 改造零残留）。"""
    content = Path("app/audit/graph.py").read_text(encoding="utf-8")
    # 匹配 \btext( 模式，排除 docstring/comment 中引用
    matches = [
        (i + 1, line)
        for i, line in enumerate(content.splitlines())
        if re.search(r"\btext\s*\(", line)
    ]
    assert matches == [], (
        "app/audit/graph.py 存在 text() 残留（T12 应已清理）:\n"
        + "\n".join(f"  L{no}: {line}" for no, line in matches)
    )
