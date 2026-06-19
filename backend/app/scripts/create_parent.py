"""create_parent CLI：创建一个父端账号（含 Family + FamilyMember）。

通过容器内 `python -m app.scripts.create_parent` 调用，复用
`core.runtime.build_runtime` 路径，与 FastAPI lifespan / ARQ worker
共享同一份进程级资源；不自建 engine，不绕过容器连库。

Usage:
    docker compose exec api python -m app.scripts.create_parent --note "张三-家长"

命令行参数：
    --note    必填。运维备注，标识该父账号的用途或负责人，写入
              `User.admin_note`。

stdout 输出（成功后一次性）：
    `phone` / `password` / `user_id` / `note`。明文密码仅此一次打印，
    需立即妥善保管。

运行时序：
    1. `cli_runtime()` 装配 `RuntimeResources` 并 yield session 与主库 Redis；
    2. `_create_parent` 插入空 `Family` → 生成唯一 `phone`（去重最多 10 次）
       与 8 位 `password` → 插入 `User`（role=parent）→ 插入 `FamilyMember` →
       `commit_with_redis` 统一提交；
    3. CLI 打印结果，`cli_runtime` 退出时反向释放资源。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.core.redis import commit_with_redis
from app.domain.accounts.models import Family, FamilyMember, User
from app.domain.auth.password import generate_password, generate_phone, hash_password
from app.scripts._common import build_arg_parser, cli_runtime, run_main

MAX_PHONE_RETRIES = 10


@dataclass(frozen=True)
class ParentInfo:
    """`_create_parent` 的返回值，供 CLI 输出或测试断言使用。"""

    phone: str
    plain_password: str
    user_id: uuid.UUID
    family_id: uuid.UUID


async def _ensure_unique_phone(db: AsyncSession, max_retries: int = MAX_PHONE_RETRIES) -> str:
    """生成未占用的父端 `phone`，最多重试 `max_retries` 次。

    通过查询 `User` 中 `phone` 相同、`role=parent` 且 `is_active=True`
    的记录判定是否冲突。

    Args:
        db: 当前会话，用于查重。
        max_retries: 最多重试次数（含首次生成），默认 `MAX_PHONE_RETRIES`。

    Returns:
        唯一可用的 4 位 phone 字符串。

    Raises:
        RuntimeError: 连续 `max_retries` 次均冲突，未能生成唯一 phone。
    """
    for _ in range(max_retries):
        phone = generate_phone()
        existing = (
            await db.execute(
                select(User).where(
                    User.phone == phone,
                    User.role == UserRole.parent,
                    User.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return phone
    raise RuntimeError(f"failed to generate unique phone after {max_retries} retries")


async def _create_parent(db: AsyncSession, redis: Redis, *, note: str) -> ParentInfo:
    """创建父端账号及其 family 边界（CLI 与测试共用入口）。

    流程：插入空 `Family` → 生成唯一 `phone` 与 8 位 `password` →
    插入 `User`（`role=parent`、`is_active=True`）→ 插入
    `FamilyMember` → `commit_with_redis` 统一提交。

    Args:
        db: 当前数据库会话。
        redis: 主库 Redis，用于 `commit_with_redis` 的 flush。
        note: 运维备注，写入 `User.admin_note`。

    Returns:
        `ParentInfo`，包含 `phone` / `plain_password` / `user_id` /
        `family_id`，供调用方决定输出格式。
    """
    family = Family()
    db.add(family)
    await db.flush()

    phone = await _ensure_unique_phone(db)
    password = generate_password()

    user = User(
        family_id=family.id,
        role=UserRole.parent,
        phone=phone,
        password_hash=hash_password(password),
        is_active=True,
        admin_note=note,
    )
    db.add(user)
    await db.flush()

    db.add(
        FamilyMember(
            family_id=family.id,
            user_id=user.id,
            role=UserRole.parent,
        )
    )

    await commit_with_redis(db, redis)

    return ParentInfo(
        phone=phone,
        plain_password=password,
        user_id=user.id,
        family_id=family.id,
    )


async def _main() -> None:
    """CLI 入口：解析参数 → 通过 `cli_runtime` 跑 `_create_parent` → 打印结果。

    由 `main()` 通过 `asyncio.run(run_main(_main))` 调度；异常路径由
    `run_main` 统一以退出码 1 终止。
    """
    parser = build_arg_parser(note_required=True)
    args = parser.parse_args()
    async with cli_runtime() as (db, redis):
        info = await _create_parent(db, redis, note=args.note)
        print("✅ parent created")
        print(f"   phone:    {info.phone}")
        print(f"   password: {info.plain_password}")
        print(f"   user_id:  {info.user_id}")
        print(f"   note:     {args.note}")
        print("⚠️  明文密码仅此一次打印，请立即妥善保管。")


def main() -> None:
    """同步入口：`asyncio.run(run_main(_main))`，供 `python -m` 触发。"""
    asyncio.run(run_main(_main))


if __name__ == "__main__":
    main()
