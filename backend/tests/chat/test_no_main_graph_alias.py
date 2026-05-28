"""main_graph 模块级单例不存在硬断言（T15 C2）。

验证 T8 工厂化后 `app.chat.graph.main_graph` 已删除（无向后兼容别名）。
"""
from __future__ import annotations


def test_no_main_graph_alias():
    """app.chat.graph 不存在 main_graph 模块级别名（T8 工厂化时已删）。"""
    import importlib

    mod = importlib.import_module("app.chat.graph")
    assert not hasattr(mod, "main_graph"), "main_graph 别名应已在 T8 工厂化时删除"
    assert "main_graph" not in mod.__dict__, (
        "main_graph 不应在模块 __dict__ 中（防 __getattr__ 动态属性误判）"
    )
