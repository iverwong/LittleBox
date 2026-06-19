"""全栈 ORM 聚合点。

显式 import 五个业务域（accounts / chat / audit / expert / notifications）
共 13 张表，与 `Base` / `BaseMixin` 一起再导出。`alembic/env.py` 必须
`from app.core.models import Base`，否则 alembic 看不到全部 13 张表，
`alembic check` 会产 DROP。

D-1 边界：本模块是零业务逻辑的纯聚合，不引任何 API 路由 / use case /
service / 测试 fixture；只透传 `core.db` 的 `Base` / `BaseMixin` 与
5 域的 models。

显式逐类 import 的原因：
- 不触发 ruff F403（`import *` 警告）；
- 类名清单与 `__all__` 一一对应，IDE 跳转 / static check 更友好。
"""

from app.core.db import Base, BaseMixin  # 透传
from app.domain.accounts.models import (
    AuthToken,
    ChildProfile,
    DataDeletionRequest,
    DeviceToken,
    Family,
    FamilyMember,
    User,
)
from app.domain.audit.models import AuditRecord, RollingSummary
from app.domain.chat.models import Message, Session
from app.domain.expert.models import DailyReport
from app.domain.notifications.models import Notification

__all__ = [
    "Base",
    "BaseMixin",
    # accounts(7)
    "Family",
    "User",
    "FamilyMember",
    "ChildProfile",
    "AuthToken",
    "DeviceToken",
    "DataDeletionRequest",
    # audit(2)
    "AuditRecord",
    "RollingSummary",
    # chat(2)
    "Session",
    "Message",
    # expert(1)
    "DailyReport",
    # notifications(1)
    "Notification",
]
