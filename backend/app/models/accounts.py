"""Transitional shim — 6.C 临时保留,6.4 整体删除。

7 张表(Family / User / ChildProfile / AuthToken / DeviceToken /
FamilyMember / DataDeletionRequest)已迁至 app.domain.accounts.models。
此处仅供现存老 import (from app.models.accounts import X) 继续可用;
新代码应直接 from app.domain.accounts.models import X。
"""

from app.domain.accounts.models import (
    AuthToken,
    ChildProfile,
    DataDeletionRequest,
    DeviceToken,
    Family,
    FamilyMember,
    User,
)

__all__ = [
    "AuthToken",
    "ChildProfile",
    "DataDeletionRequest",
    "DeviceToken",
    "Family",
    "FamilyMember",
    "User",
]
