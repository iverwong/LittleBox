"""CLI 专用运行时：共享 ArgParser 骨架 + async runner + cli_runtime()。"""

from __future__ import annotations

import argparse
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

if TYPE_CHECKING:
    from argparse import ArgumentParser


@asynccontextmanager
async def cli_runtime() -> AsyncIterator[tuple[AsyncSession, Redis]]:
    """CLI 专用：手动建 (AsyncSession, Redis)；退出时一并释放。

    decode_responses=True 必须与生产 redis_client 保持一致，
    否则 commit_with_redis flush 后 resolve_token 回填路径会遇 bytes。
    """
    engine = create_async_engine(settings.database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    session = session_maker()
    try:
        yield session, redis
    finally:
        await session.close()
        await redis.aclose()
        await engine.dispose()


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
