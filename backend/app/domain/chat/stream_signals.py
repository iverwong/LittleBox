"""进程级 stop event 登记表——主对话流的可控停流信号。

`running_streams` 是 in-process `dict[sid -> asyncio.Event]`,用于跨
HTTP 请求边界(me.py generator 在 create_task 前注册 event,
之后 stop_session 路由对其调 set)传递 stop signal。

Cleanup contract: `running_streams` entry 由 caller 在 finally
块 `pop(sid, None)` 清理(见 `app.domain.chat.pipeline.run_llm_pipeline`)。
"""

from __future__ import annotations

import asyncio

running_streams: dict[str, asyncio.Event] = {}
"""模块级活跃流 stop event 登记表。key 为 session_id(str),value 为 asyncio.Event。

注册时机:消费协程创建前;清理时机:消费协程 finally 块。
跨进程 stop signaling 不支持(单 uvicorn worker 部署假设)。
"""
