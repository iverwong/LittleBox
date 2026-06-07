"""CLI 专用运行时：共享 ArgParser 骨架 + async runner + cli_runtime()。

cli_runtime 用 core.runtime.build_runtime 装配进程级容器（D-6, Phase 5.1），
与 FastAPI lifespan / ARQ worker 共用一份 engine / audit_redis / arq_pool /
main_graph / audit_graph；主库 redis（db=0）走 D-5A.1 例外——build_runtime
不暴露主库 redis（那是 lifespan 的 _redis 单例），CLI 无从复用，自建是唯一
正确解。该例外已正式登记为边界铁律 4「禁自建 redis」的已知例外。

已知行为 delta（D-5A.4）：build_runtime 的 engine 带 ``pool_pre_ping=True``，
原 cli_runtime 自建 engine 没有。对长生命周期 CLI 无害，接受。模板 B 单列
披露，不并入「与原逻辑一致」。

D-5A.2：draw_graph.py 不走 cli_runtime（不调，纯 import + 工厂调用，无 DB/Redis
需求，强行拉它走 build_runtime 会无谓启动 arq_pool，违反 KISS）。

退出顺序（无论 yield 体是否抛异常，finally 强制）：
  1. redis_main.aclose()（主库 db=0）
  2. teardown_runtime(rr)：arq_pool → audit_redis → db_engine 反向关
session 经 ``async with rr.db_session_factory()`` 自动关。
"""

from __future__ import annotations

import argparse
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.runtime import build_runtime, teardown_runtime

if TYPE_CHECKING:
    from argparse import ArgumentParser

    from app.core.runtime import RuntimeResources


@asynccontextmanager
async def cli_runtime() -> AsyncIterator[tuple[AsyncSession, Redis]]:
    """CLI 专用：build_runtime 拿进程级资源 + 自建主库 db=0 redis；退出时统一释放。

    yield (session, redis)：
      - session：由 ``rr.db_session_factory()`` 产出，生命周期绑定 async with。
      - redis  ：主库 db=0；此 redis 与 lifespan 的 _redis 是两个 client，
                行为字节一致（同 URL、同 ``decode_responses=True``、不传
                ``encoding``——lifespan 版才传 ``encoding="utf-8"``，CLI
                必须与原 cli_runtime 一致照抄，不可顺手对齐 lifespan）。
                build_runtime 不暴露主库 redis，这是 D-5A.1 边界铁律 4
                「禁自建 redis」的已知例外。

    失败兜底：yield 体抛异常时，finally 仍会先 ``aclose(redis_main)`` 再
    ``teardown_runtime(rr)``，保证 arq_pool / audit_redis / db_engine 不泄漏。
    """
    rr: RuntimeResources = await build_runtime(settings)
    # 主库 redis：与原 cli_runtime 字节一致——不传 encoding（仅 lifespan 版传）
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
    """共享 ArgParser 骨架。"""
    parser = argparse.ArgumentParser(prog="app.scripts")
    if note_required:
        parser.add_argument("--note", required=True, help="运维备注")
    if phone_required:
        parser.add_argument("--phone", required=True, help="父账号手机号")
    return parser


async def run_main(main_func):
    """统一 async runner。"""
    try:
        await main_func()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
