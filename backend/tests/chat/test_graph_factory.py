"""build_main_graph 工厂 + route_by_risk 2 分支回归（T15）。

去重边界（H5）：不重复 5 节点拓扑断言（test_graph.py 已有），
仅覆盖工厂调用 + CompiledStateGraph 类型 + 2 分支 keys 完整性。
"""
from __future__ import annotations

from app.domain.chat.graph import build_main_graph, route_by_risk


def test_build_main_graph_returns_compiled():
    """build_main_graph() 返回 CompiledStateGraph 实例。"""
    from langgraph.graph.state import CompiledStateGraph

    graph = build_main_graph()
    assert isinstance(graph, CompiledStateGraph)


def test_route_by_risk_two_branches():
    """route_by_risk 返回 {crisis, main} 二分枝。"""
    from app.domain.chat.state import MainDialogueState

    state: MainDialogueState = {
        "messages": [],
        "audit_state": {
            "crisis_locked": False,
            "crisis_detected": False,
            "guidance": None,
        },
        "turn_number": 1,
    }

    # crisis_locked → crisis
    state["audit_state"]["crisis_locked"] = True
    assert route_by_risk(state) == "crisis"

    # all default → main
    state["audit_state"] = {"crisis_locked": False, "crisis_detected": False,
                            "guidance": None}
    assert route_by_risk(state) == "main"
