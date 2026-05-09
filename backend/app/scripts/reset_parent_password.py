"""reset_parent_password CLI：重置父账号密码并吊销所有活跃 token。

Usage:
    docker compose exec api python -m app.scripts.reset_parent_password --phone abcd

Arguments:
    --phone    必填。要重置密码的父账号手机号（4 位字母）。

Exit codes:
    0       成功重置，stdout 打印新密码。
    非 0    失败（phone 不存在或非活跃 parent）；stderr 打印错误信息。

Output:
    成功后在 stdout 打印 phone / user_id / 新密码。
    ⚠️  明文密码仅此一次，请立即妥善保管。

Details:
    - 只作用于 is_active=True 且 role=parent 的账号（fail closed）。
    - 自动生成新 8 位密码（去 i/l/o），覆盖原有 password_hash。
    - 重置后立即吊销该 parent 所有活跃 token（DB revoked_at + Redis 双清）。
    - 走 commit_with_redis 统一入口，不裸 db.commit()。
    - 运行在 CLI 专用 cli_runtime() 中，不复用 FastAPI 全局 Redis 连接。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.password import generate_password, hash_password
from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import revoke_all_active_tokens
from app.models.accounts import User
from app.models.enums import UserRole
from app.scripts._common import build_arg_parser, cli_runtime, run_main


@dataclass(frozen=True)
class ResetResult:
    """_reset_password 的返回值。CLI 与测试共用。"""
    phone: str
    plain_password: str
    user_id: uuid.UUID


async def _reset_password(db: AsyncSession, redis: Redis, *, phone: str) -> ResetResult:
    """重置父账号密码并吊销所有活跃 token。CLI 与测试共用入口，不含 IO 副作用。

    返回 ResetResult 供调用方输出。phone 不存在或非活跃 parent 时抛出 ValueError。
    """
    stmt = select(User).where(
        User.phone == phone,
        User.role == UserRole.parent,
        User.is_active.is_(True),
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise ValueError(f"no active parent found with phone {phone}")

    new_password = generate_password()
    user.password_hash = hash_password(new_password)
    await revoke_all_active_tokens(db, user.id)
    await commit_with_redis(db, redis)

    return ResetResult(phone=phone, plain_password=new_password, user_id=user.id)


async def _main() -> None:
    parser = build_arg_parser(phone_required=True)
    args = parser.parse_args()
    async with cli_runtime() as (db, redis):
        result = await _reset_password(db, redis, phone=args.phone)
        print("✅ password reset")
        print(f"   phone:    {result.phone}")
        print(f"   user_id:  {result.user_id}")
        print(f"   password: {result.plain_password}")
        print("⚠️  明文密码仅此一次打印，请立即妥善保管。")


def main() -> None:
    asyncio.run(run_main(_main))


if __name__ == "__main__":
    main()
