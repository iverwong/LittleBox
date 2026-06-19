"""reset_parent_password CLI：重置父账号密码并吊销该账号所有活跃 token。

通过容器内 `python -m app.scripts.reset_parent_password` 调用，复用
`core.runtime.build_runtime` 路径，与 FastAPI lifespan / ARQ worker
共享同一份进程级资源；不自建 engine，不绕过容器连库。

Usage:
    docker compose exec api python -m app.scripts.reset_parent_password --phone abcd

命令行参数：
    --phone    必填。要重置的父账号手机号（4 位字母）。

退出码：
    0       成功重置，stdout 打印新密码。
    非 0    失败（phone 不存在或非活跃 parent），stderr 输出错误信息。

stdout 输出（成功后一次性）：
    `phone` / `user_id` / 新 `password`。明文密码仅此一次打印，需立即
    妥善保管。

运行时序：
    1. `cli_runtime()` 装配 `RuntimeResources` 并 yield session 与主库 Redis；
    2. `_reset_password` 按 `phone` + `role=parent` + `is_active=True`
       查 `User`，缺失则抛 `ValueError`；
    3. 生成新密码 → 覆盖 `password_hash` → 调用
       `revoke_all_active_tokens` 吊销该账号所有 token（DB `revoked_at`
       与 Redis 双清）→ `commit_with_redis` 统一提交；
    4. CLI 打印结果，`cli_runtime` 退出时反向释放资源。
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
from app.domain.accounts.models import User
from app.domain.auth.password import generate_password, hash_password
from app.domain.auth.tokens import revoke_all_active_tokens
from app.scripts._common import build_arg_parser, cli_runtime, run_main


@dataclass(frozen=True)
class ResetResult:
    """`_reset_password` 的返回值，供 CLI 输出或测试断言使用。"""

    phone: str
    plain_password: str
    user_id: uuid.UUID


async def _reset_password(db: AsyncSession, redis: Redis, *, phone: str) -> ResetResult:
    """重置父账号密码并吊销该账号所有活跃 token（CLI 与测试共用入口）。

    Args:
        db: 当前数据库会话。
        redis: 主库 Redis，用于 `commit_with_redis` 的 flush。
        phone: 目标父账号手机号（4 位字母）。

    Returns:
        `ResetResult`，包含 `phone` / `plain_password` / `user_id`。

    Raises:
        ValueError: phone 不存在或非 `role=parent` 且 `is_active=True`
            的账号。
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
    """CLI 入口：解析参数 → 通过 `cli_runtime` 跑 `_reset_password` → 打印结果。

    由 `main()` 通过 `asyncio.run(run_main(_main))` 调度；异常路径由
    `run_main` 统一以退出码 1 终止。
    """
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
    """同步入口：`asyncio.run(run_main(_main))`，供 `python -m` 触发。"""
    asyncio.run(run_main(_main))


if __name__ == "__main__":
    main()
