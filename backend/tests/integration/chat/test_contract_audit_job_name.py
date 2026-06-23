"""Step 9 · 契约：入队名 ↔ 注册名一致（修复后 GREEN）。

enqueue_audit 在 `app.domain.chat.usecase` 中调用的 job 名应与
WORKER_SETTINGS 中注册的函数路径完全一致。

修复后匹配：
  usecase.py:    AUDIT_JOB_NAME = "app.worker.run_audit"
  worker.py:     WORKER_SETTINGS["functions"] = ["app.worker.run_audit", ...]

⚠️ 若移动或重命名 worker 模块，两处都必须同步更新。
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
def test_audit_job_name_contract() -> None:
    """入队名应与注册名一致。

    纯契约测试，无需 DB / Redis / app 等 fixture，
    从 `usecase.py` 与 `worker.py` 各取一次字面量做双侧断言
    （不写死任何一侧的字符串,避免单侧漂移盲区）。
    """
    from app.worker import WORKER_SETTINGS
    from app.domain.chat.usecase import AUDIT_JOB_NAME

    registered = WORKER_SETTINGS["functions"][0]

    assert AUDIT_JOB_NAME == registered, (
        f"usecase.py AUDIT_JOB_NAME='{AUDIT_JOB_NAME}' "
        f"!= worker.py 注册名 '{registered}'。\n"
        "两处必须逐字一致,否则 worker 日志 'function <name> not found'。"
    )
