"""build_audit_graph 无参工厂回归（T16 H5）。

去重边界：不重复 7 路径覆盖（test_audit_graph.py 已有），仅做：
- build_audit_graph() 无参可调
- 返回值是 CompiledStateGraph
- get_graph().nodes 含 4 个注册节点名
"""
from __future__ import annotations

import pytest
from app.audit.graph import build_audit_graph

pytestmark = pytest.mark.audit


def test_build_audit_graph_no_arg():
    """build_audit_graph() 无参返回 CompiledStateGraph。"""
    from langgraph.graph.state import CompiledStateGraph

    graph = build_audit_graph()
    assert isinstance(graph, CompiledStateGraph)


def test_audit_graph_has_four_nodes():
    """audit 图含 4 个注册节点：load_context / audit_llm_call / audit_tools / write_results。"""
    graph = build_audit_graph()
    node_names = set(graph.get_graph().nodes.keys())
    expected = {"load_context", "audit_llm_call", "audit_tools", "write_results"}
    assert expected.issubset(node_names), (
        f"Missing nodes: {expected - node_names}"
    )
