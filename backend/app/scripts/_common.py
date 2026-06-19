"""CLI 脚本的共用工具：`cli_runtime` / `build_arg_parser` / `run_main`。

`cli_runtime` 是异步上下文管理器，复用 `core.runtime.build_runtime`
路径装配进程级资源（`db_engine` / `audit_redis` / `arq_pool` /
`main_graph` / `audit_graph`），与 FastAPI lifespan 和 ARQ worker
保持同一份底层连接。主库 Redis（`db=0`）由 CLI 自建 —— `build_runtime`
不暴露主库 Redis（那是 lifespan 的 `_redis` 单例），自建是当前唯一的
正确路径。

退出顺序由 `finally` 强制保证：先关闭自建主库 Redis，再调用
`teardown_runtime(rr)` 反向关闭 `arq_pool` / `audit_redis` /
`db_engine`。`AsyncSession` 由 `async with rr.db_session_factory()`
托管，随上下文退出自动关闭。

`draw_graph.py` 不走 `cli_runtime`，因为它仅做 import 与工厂调用，
不涉及 DB / Redis，强行装配会无谓启动 `arq_pool`。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.runtime import build_runtime, teardown_runtime

if TYPE_CHECKING:
    from argparse import ArgumentParser

    from app.core.runtime import RuntimeResources


@asynccontextmanager
async def cli_runtime() -> AsyncGenerator[tuple[AsyncSession, Redis], None]:
    """装配 CLI 进程级资源并 yield `(session, redis)`，退出时统一释放。

    Returns:
        包含两个元素的元组：
            - `session`：由 `rr.db_session_factory()` 产出的 `AsyncSession`，
              生命周期绑定 `async with` 块。
            - `redis`：主库 `db=0` 的 Redis 客户端，与 lifespan 的
              `_redis` 行为字节一致（同一 URL、`decode_responses=True`、
              不传 `encoding`），仅 CLI 用途，与 lifespan 客户端互不共享。

    Raises:
        透传 `yield` 体内的异常；`finally` 仍保证关闭主库 Redis 与
        `RuntimeResources`，不泄漏 `arq_pool` / `audit_redis` / `db_engine`。
    """
    rr: RuntimeResources = await build_runtime(settings)
    # 主库 redis：不传 encoding（仅 lifespan 版传），保持 CLI 客户端一致
    redis_main = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with rr.db_session_factory() as session:
            yield session, redis_main
    finally:
        await redis_main.aclose()
        await teardown_runtime(rr)


def build_arg_parser(
    *,
    note_required: bool = False,
    phone_required: bool = False,
) -> "ArgumentParser":
    """构造共享的 `argparse.ArgumentParser` 骨架。

    Args:
        note_required: 为真时追加必填的 `--note` 参数（运维备注）。
        phone_required: 为真时追加必填的 `--phone` 参数（父账号手机号）。

    Returns:
        配置好的 `ArgumentParser` 实例，可继续追加本脚本私有参数。
    """
    parser = argparse.ArgumentParser(prog="app.scripts")
    if note_required:
        parser.add_argument("--note", required=True, help="运维备注")
    if phone_required:
        parser.add_argument("--phone", required=True, help="父账号手机号")
    return parser


async def run_main(main_func):
    """统一的异步入口执行器：捕获异常并以退出码 1 终止。

    Args:
        main_func: 异步协程函数，调用时不传参。
    """
    try:
        await main_func()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
