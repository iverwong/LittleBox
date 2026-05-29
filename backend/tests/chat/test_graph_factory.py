"""build_main_graph 工厂 + route_by_risk 4 分支回归（T15）。

去重边界（H5）：不重复 7 节点拓扑断言（test_graph.py::test_graph_has_7_nodes 已有），
仅覆盖工厂调用 + CompiledStateGraph 类型 + 4 分支 keys 完整性。
"""
from __future__ import annotations

import pytest

from app.chat.graph import build_main_graph, route_by_risk

def test_build_main_graph_returns_compiled():
    """build_main_graph() 返回 CompiledStateGraph 实例。"""
    from langgraph.graph.state import CompiledStateGraph

    graph = build_main_graph()
    assert isinstance(graph, CompiledStateGraph)


def test_route_by_risk_four_branches():
    """route_by_risk 返回 {crisis, redline, guidance, main} 四分支（D-patch0-12）。

    计划 §3 字面 "safe" 判定为计划误差（H2）。
    """
    from app.chat.state import MainDialogueState

    state: MainDialogueState = {
        "messages": [],
        "audit_state": {
            "crisis_locked": False,
            "crisis_detected": False,
            "redline_triggered": False,
            "guidance": None,
        },
        "generated_token_count": 0,
        "client_alive": True,
        "user_stop_requested": False,
        "turn_number": 1,
    }

    # crisis_locked → crisis
    state["audit_state"]["crisis_locked"] = True
    assert route_by_risk(state) == "crisis"

    # crisis_detected → crisis
    state["audit_state"] = {"crisis_locked": False, "crisis_detected": True,
                            "redline_triggered": False, "guidance": None}
    assert route_by_risk(state) == "crisis"

    # redline_triggered → redline
    state["audit_state"] = {"crisis_locked": False, "crisis_detected": False,
                            "redline_triggered": True, "guidance": None}
    assert route_by_risk(state) == "redline"

    # guidance non-None → guidance
    state["audit_state"] = {"crisis_locked": False, "crisis_detected": False,
                            "redline_triggered": False, "guidance": "引导文案"}
    assert route_by_risk(state) == "guidance"

    # all default → main
    state["audit_state"] = {"crisis_locked": False, "crisis_detected": False,
                            "redline_triggered": False, "guidance": None}
    assert route_by_risk(state) == "main"
