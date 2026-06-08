"""进程级 stop event 登记表 — 主对话流的可控停流信号。

`running_streams` 是 in-process `dict[sid -> asyncio.Event]`,用于跨
HTTP 请求边界(me.py generator 在 create_task 后向注册 event 调 set)
传递 stop signal。

跨进程 stop signaling NOT implemented(M6 单 uvicorn worker 部署假设)。

Cleanup contract: `running_streams` entry 由 caller 在 generator finally
块 `pop(sid, None)` 清理(见 `app.domain.chat.pipeline.run_llm_pipeline`)。
Step 2 不测清理;Step 8c 集成测试覆盖。

Redis 锁原语 / Lua 部署契约见 `app.core.locks`(拆 D-1 边界)。
"""

from __future__ import annotations

import asyncio

running_streams: dict[str, asyncio.Event] = {}
"""Module-level registry of active stream stop events. key=session_id, value=asyncio.Event."""
