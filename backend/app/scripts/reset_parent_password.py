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
import sys

from sqlalchemy import select

from app.auth.password import generate_password, hash_password
from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import revoke_all_active_tokens
from app.models.accounts import User
from app.models.enums import UserRole
from app.scripts._common import build_arg_parser, cli_runtime, run_main


async def _reset_password(phone: str) -> None:
    async with cli_runtime() as (db, redis):
        # 1. 查询活跃 parent（role + is_active 双约束）
        stmt = select(User).where(
            User.phone == phone,
            User.role == UserRole.parent,
            User.is_active.is_(True),
        )
        user = (await db.execute(stmt)).scalar_one_or_none()
        if user is None:
            print(f"ERROR: no active parent found with phone {phone}", file=sys.stderr)
            raise SystemExit(1)

        # 2. 生成新密码（此时不打印）
        new_password = generate_password()

        # 3. 更新 password_hash
        user.password_hash = hash_password(new_password)

        # 4. 吊销所有活跃 token（DB + Redis 双清）
        await revoke_all_active_tokens(db, user.id)

        # 5. 统一 commit
        await commit_with_redis(db, redis)

        # 6. 提交成功后打印密码（只打印一次）
        print("✅ password reset")
        print(f"   phone:    {phone}")
        print(f"   user_id:  {user.id}")
        print(f"   password: {new_password}")
        print("⚠️  明文密码仅此一次打印，请立即妥善保管。")


def main() -> None:
    parser = build_arg_parser(phone_required=True)
    args = parser.parse_args()
    asyncio.run(run_main(lambda: _reset_password(args.phone)))


if __name__ == "__main__":
    main()
