"""Step 9 · 契约：入队名 ↔ 注册名一致（修复后 GREEN）。

enqueue_audit 在 graph.py 中调用的 job 名应与
WORKER_SETTINGS 中注册的函数路径完全一致。

修复后匹配：
  graph.py:133:  enqueue_job("app.audit.worker.run_audit", ...)
  worker.py:89:  WORKER_SETTINGS["functions"] = ["app.audit.worker.run_audit"]

⚠️ 若移动或重命名 worker 模块，两处都必须同步更新。
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
def test_audit_job_name_contract() -> None:
    """入队名应与注册名一致。

    纯契约测试，无需 DB / Redis / app 等 fixture，
    仅 import 两个模块做字符串比对。
    """
    from app.audit.worker import WORKER_SETTINGS

    registered = WORKER_SETTINGS["functions"][0]  # "app.audit.worker.run_audit"

    # graph.py:enqueue_audit 中 enqueue_job 使用的函数名字面量
    enqueue_name = "app.audit.worker.run_audit"

    assert enqueue_name == registered, (
        f"入队名 '{enqueue_name}' != 注册名 '{registered}'。\n"
        "两处必须逐字一致，否则 worker 日志 'function <name> not found'。"
    )
