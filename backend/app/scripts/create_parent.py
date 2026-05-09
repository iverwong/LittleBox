"""create_parent CLI：创建父账号 family + user + family_members。

Usage:
    docker compose exec api python -m app.scripts.create_parent --note "张三-家长"

Arguments:
    --note     必填。运维备注，用于标识这个父账号的用途或负责人。

Output:
    成功后在 stdout 打印 phone / password / user_id / note。
    ⚠️  明文密码仅此一次，请立即妥善保管。

Details:
    - phone：4 位小写字母（去 i/l/o），全局唯一 active parent 最多重试 10 次。
    - password：8 位小写字母（去 i/l/o），永不打印第二次。
    - 自动创建 Family + FamilyMember，保证 family 边界完整性。
    - 即使无 Redis 写操作，也走 commit_with_redis 统一入口。
    - 运行在 CLI 专用 cli_runtime() 中，不复用 FastAPI 全局 Redis 连接。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.password import generate_password, generate_phone, hash_password
from app.auth.redis_ops import commit_with_redis
from app.models.accounts import Family, FamilyMember, User
from app.models.enums import UserRole
from app.scripts._common import build_arg_parser, cli_runtime, run_main

MAX_PHONE_RETRIES = 10


@dataclass(frozen=True)
class ParentInfo:
    """_create_parent 的返回值。CLI 与测试共用。"""
    phone: str
    plain_password: str
    user_id: uuid.UUID
    family_id: uuid.UUID


async def _ensure_unique_phone(db: AsyncSession, max_retries: int = MAX_PHONE_RETRIES) -> str:
    """生成唯一 phone，若撞已有 active parent 则重试最多 max_retries 次。"""
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
    """创建 parent 账户。CLI 与测试共用入口，不含 IO 副作用。

    返回 ParentInfo 供调用方决定输出格式（CLI 打印 stdout / 测试断言）。
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
    asyncio.run(run_main(_main))


if __name__ == "__main__":
    main()
