"""create_parent CLI：创建父账号 family + user + family_members。"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.auth.password import generate_password, generate_phone, hash_password
from app.auth.redis_ops import commit_with_redis
from app.models.accounts import Family, FamilyMember, User
from app.models.enums import UserRole
from app.scripts._common import build_arg_parser, cli_runtime, run_main

MAX_PHONE_RETRIES = 10


async def _ensure_unique_phone(db, max_retries: int = MAX_PHONE_RETRIES) -> str:
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


async def _create_parent(note: str) -> None:
    async with cli_runtime() as (db, redis):
        # 1. 新建 Family
        family = Family()
        db.add(family)
        await db.flush()

        # 2. 生成 phone（防碰撞重试）
        phone = await _ensure_unique_phone(db)

        # 3. 生成明文 password（此时不打印）
        password = generate_password()

        # 4. 新建 User(role=parent)
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

        # 5. 新建 FamilyMember
        db.add(
            FamilyMember(
                family_id=family.id,
                user_id=user.id,
                role=UserRole.parent,
            )
        )

        # 6. commit（无 Redis op 也走统一入口）
        await commit_with_redis(db, redis)

        # 7. 提交成功后打印密码（只打印一次）
        print("✅ parent created")
        print(f"   phone:    {phone}")
        print(f"   password: {password}")
        print(f"   user_id:  {user.id}")
        print(f"   note:     {note}")
        print("⚠️  明文密码仅此一次打印，请立即妥善保管。")


def main() -> None:
    parser = build_arg_parser(note_required=True)
    args = parser.parse_args()
    asyncio.run(run_main(lambda: _create_parent(args.note)))


if __name__ == "__main__":
    main()
