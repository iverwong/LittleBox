"""父端 child profile 资源:``/api/v1/child-profiles``。

父账号读取与部分更新子账号的配置(基础信息 + 关注点 + 高级配置
sensitivity / custom_redlines)。仅 parent 可访问,family 归属通过
``load_child_profile_in_family`` 焊进同一条 WHERE 防 IDOR。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import get_redis
from app.core.time import age_at
from app.domain.accounts.models import ChildProfile
from app.domain.accounts.schemas import (
    ChildProfileDetail,
    SensitivityConfig,
    UpdateChildProfileRequest,
)
from app.domain.accounts.service import (
    load_child_profile_in_family,
    update_child_profile,
)
from app.domain.auth.deps import require_parent

router = APIRouter(prefix="/api/v1/child-profiles", tags=["child-profiles"])


def _to_detail(profile: ChildProfile) -> ChildProfileDetail:
    """把 ChildProfile ORM 组装为父端响应体。

    Args:
        profile: 已加载的 `ChildProfile`。

    Returns:
        填好 age 换算与 sensitivity 规整的 `ChildProfileDetail`。
    """
    return ChildProfileDetail(
        child_user_id=profile.child_user_id,
        nickname=profile.nickname,
        gender=profile.gender.value,
        birth_date=profile.birth_date,
        age=age_at(profile.birth_date, tz="Asia/Shanghai"),
        concerns=profile.concerns,
        sensitivity=SensitivityConfig(**profile.sensitivity) if profile.sensitivity else None,
        custom_redlines=profile.custom_redlines,
    )


@router.get("/{child_user_id}", response_model=ChildProfileDetail)
async def get_child_profile(
    child_user_id: uuid.UUID,
    parent: Annotated[..., Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChildProfileDetail:
    """父端读取子账号配置,``GET /api/v1/child-profiles/{child_user_id}``。仅 parent。

    family 归属与 child 角色 + is_active 在同一条 WHERE 内校验,任一不满足
    均统一抛 404,不暴露存在性。

    Args:
        child_user_id: 目标子账号 `User.id`(path param)。
        parent: 当前父账号上下文(``Depends(require_parent)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。

    Returns:
        ``ChildProfileDetail``:父端读回的全字段配置。

    Raises:
        HTTPException ``status.HTTP_401_UNAUTHORIZED``:缺失 / 非法 Bearer token。
        HTTPException ``status.HTTP_403_FORBIDDEN``:非 parent 角色。
        HTTPException ``status.HTTP_404_NOT_FOUND``:child 不存在或非本 family。
    """
    profile = await load_child_profile_in_family(
        db, child_user_id=child_user_id, family_id=parent.family_id
    )
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "child not found in family")
    return _to_detail(profile)


@router.patch("/{child_user_id}", response_model=ChildProfileDetail)
async def patch_child_profile(
    child_user_id: uuid.UUID,
    payload: UpdateChildProfileRequest,
    parent: Annotated[..., Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ChildProfileDetail:
    """父端部分更新子账号配置,``PATCH /api/v1/child-profiles/{child_user_id}``。仅 parent。

    委托 ``accounts.service.update_child_profile`` 执行 PATCH 语义:
    字段未传 = 不动,可空字段传 null = 清空,sensitivity 整体替换。
    ``commit_with_redis`` 落盘,后续 chat_stream 自然读到新配置。

    Args:
        child_user_id: 目标子账号 `User.id`(path param)。
        payload: ``UpdateChildProfileRequest``(部分更新请求体)。
        parent: 当前父账号上下文(``Depends(require_parent)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。
        redis: 主业务 Redis(``Depends(get_redis)``)。

    Returns:
        ``ChildProfileDetail``:更新后的全字段配置。

    Raises:
        HTTPException ``status.HTTP_401_UNAUTHORIZED``:缺失 / 非法 Bearer token。
        HTTPException ``status.HTTP_403_FORBIDDEN``:非 parent 角色。
        HTTPException ``status.HTTP_404_NOT_FOUND``:child 不存在或非本 family。
    """
    profile = await update_child_profile(
        db,
        redis,
        parent=parent,
        child_user_id=child_user_id,
        payload=payload,
    )
    return _to_detail(profile)
