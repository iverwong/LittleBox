"""子账号管理路由:``/api/v1/children``。

父端对子账号的 CRUD:创建子账号、列出本家庭所有子账号、吊销某子账号
全部活跃 token、硬删子账号。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.enums import UserRole
from app.core.redis import commit_with_redis, get_redis
from app.domain.accounts.models import AuthToken, ChildProfile, User
from app.domain.accounts.schemas import (
    ChildSummary,
    CreateChildRequest,
    CurrentAccount,
    ListChildrenResponse,
)
from app.domain.accounts.service import (
    create_child as create_child_service,
)
from app.domain.accounts.service import (
    hard_delete_child,
)
from app.domain.auth.deps import require_parent
from app.domain.auth.tokens import revoke_all_active_tokens

router = APIRouter(prefix="/api/v1/children", tags=["children"])


@router.post("", response_model=ChildSummary, status_code=201)
async def create_child(
    payload: CreateChildRequest,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ChildSummary:
    """创建子账号,``POST /api/v1/children``。仅 parent。

    委托 ``accounts.service.create_child`` 执行跨表事务(``users`` /
    ``child_profiles`` / ``family_members`` + 家庭 child 数量上限校验
    + 行级锁)。HTTP 协议层只负责依赖注入与返回类型。

    Args:
        payload: ``CreateChildRequest``,含 nickname / age / gender。
        parent: 当前父账号上下文(``Depends(require_parent)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。
        redis: 主业务 Redis(``Depends(get_redis)``)。

    Returns:
        ``ChildSummary``:新建 child 的摘要,``is_bound`` 恒为 ``False``。

    Raises:
        HTTPException ``status.HTTP_409_CONFLICT``:家庭子账号数已达上限
            (``ChildLimitReached``)。
    """
    return await create_child_service(db, redis, parent=parent, payload=payload)


@router.get("", response_model=ListChildrenResponse)
async def list_children(
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ListChildrenResponse:
    """父账号查询本 family 下所有 child,``GET /api/v1/children``。仅 parent。

    ``is_bound`` 通过 ``EXISTS`` 动态构造(子查询查 ``auth_tokens`` 的未
    撤销行),不落库。每次请求实时计算,新签 / 吊销 token 后下一次
    请求即反映。

    Args:
        parent: 当前父账号上下文(``Depends(require_parent)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。

    Returns:
        ``ListChildrenResponse``:本 family 的 child 列表,按
        ``ChildProfile.created_at``、``ChildProfile.id`` 升序。

    Raises:
        HTTPException ``status.HTTP_401_UNAUTHORIZED``:缺失 / 非法 Bearer token。
        HTTPException ``status.HTTP_403_FORBIDDEN``:非 parent 角色。
    """
    # is_bound 子查询:存在未撤销的 auth_token 即视为已绑定
    is_bound_sub = (
        exists()
        .where(AuthToken.user_id == User.id, AuthToken.revoked_at.is_(None))
        .correlate(User)
        .label("is_bound")
    )

    stmt = (
        select(
            User.id,
            ChildProfile.nickname,
            ChildProfile.birth_date,
            ChildProfile.gender,
            ChildProfile.created_at,
            is_bound_sub,
        )
        .outerjoin(ChildProfile, ChildProfile.child_user_id == User.id)
        .where(User.family_id == parent.family_id, User.role == UserRole.child)
        .order_by(ChildProfile.created_at, ChildProfile.id)
    )
    rows = (await db.execute(stmt)).fetchall()
    return ListChildrenResponse(
        children=[
            ChildSummary(
                id=row.id,
                nickname=row.nickname,
                birth_date=row.birth_date,
                gender=row.gender,
                is_bound=row.is_bound,
            )
            for row in rows
        ]
    )


@router.post("/{child_user_id}/revoke-tokens", status_code=204)
async def revoke_child_tokens(
    child_user_id: uuid.UUID,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """吊销指定 child 的全部活跃 token,``POST /api/v1/children/{id}/revoke-tokens``。仅 parent。

    家庭边界:目标 child 必须属于当前 parent 所在的 family 且 ``is_active``。

    Args:
        child_user_id: 目标 child 的 User.id(path param)。
        parent: 当前父账号上下文(``Depends(require_parent)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。
        redis: 主业务 Redis(``Depends(get_redis)``)。

    Returns:
        无。

    Raises:
        HTTPException ``status.HTTP_404_NOT_FOUND``:child 不存在、非本
            family、角色不是 child 或已停用(统一 404,不区分原因)。
    """
    stmt = select(User).where(
        User.id == child_user_id,
        User.role == UserRole.child,
        User.family_id == parent.family_id,
        User.is_active.is_(True),
    )
    child = (await db.execute(stmt)).scalar_one_or_none()
    if child is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "child not found in family")
    await revoke_all_active_tokens(db, child.id)
    await commit_with_redis(db, redis)


@router.delete("/{child_user_id}", status_code=204)
async def delete_child(
    child_user_id: uuid.UUID,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """硬删 child 账号及全部关联数据,``DELETE /api/v1/children/{id}``。仅 parent。

    委托 ``accounts.service.hard_delete_child`` 执行:DB CASCADE 删
    ``auth_tokens`` / ``sessions`` / ``messages`` / ``audit_records`` /
    ``rolling_summaries`` / ``daily_reports`` / ``notifications`` /
    ``device_tokens`` / ``family_members`` + 清空 Redis auth 缓存 +
    写 ``DataDeletionRequest`` 审计记录。

    错误码矩阵:

    - 401:未登录(``get_current_account`` 抛)
    - 403:非 parent 角色(``require_parent`` 抛)
    - 404:目标 child 不存在 / 非本 family / 角色不是 child(不暴露存在性)
    - 204:成功

    Args:
        child_user_id: 目标 child 的 User.id(path param)。
        parent: 当前父账号上下文(``Depends(require_parent)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。
        redis: 主业务 Redis(``Depends(get_redis)``)。

    Returns:
        无。

    Raises:
        HTTPException ``status.HTTP_404_NOT_FOUND``:child 不存在、非本
            family、角色不是 child 或已停用。
    """
    stmt = select(User).where(
        User.id == child_user_id,
        User.role == UserRole.child,
        User.family_id == parent.family_id,
        User.is_active.is_(True),
    )
    child = (await db.execute(stmt)).scalar_one_or_none()
    if child is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "child not found in family")

    await hard_delete_child(db, child_user_id=child_user_id, requested_by=parent.id)
    await commit_with_redis(db, redis)
