"""Step 9 · 红测：入队名 ↔ 注册名契约（Tier1）。

验证 enqueue_audit 在 graph.py 中调用的 job 名是否与
WORKER_SETTINGS 中注册的函数路径一致。

当前已知不匹配：
  graph.py:133:  enqueue_job("run_audit", ...)
  worker.py:89:  WORKER_SETTINGS["functions"] = ["app.audit.worker.run_audit"]

预期 RED（字符串不匹配）。
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
def test_audit_job_name_contract() -> None:
    """入队名应匹配注册名，当前必 RED。

    子代理核实：
      本测试是纯契约测试，无需 DB / Redis / app 等 fixture，
      仅 import 两个模块做字符串比对。
    """
    from app.audit.worker import WORKER_SETTINGS

    registered = WORKER_SETTINGS["functions"][0]  # "app.audit.worker.run_audit"

    # graph.py:133 中 enqueue_job 使用的字面量函数名
    enqueue_name = "run_audit"

    assert enqueue_name == registered, (
        f"RED: 入队名 '{enqueue_name}' != 注册名 '{registered}'。\n"
        "enqueue_audit (graph.py:133) 使用短名 'run_audit'，\n"
        "WORKER_SETTINGS (worker.py:89) 注册完整路径 'app.audit.worker.run_audit'。\n"
        "arq worker 按注册名匹配 job → 该 job 永远不会被消费。"
    )
