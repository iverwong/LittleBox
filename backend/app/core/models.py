"""全栈 ORM 聚合点。

6.D 新建:把 5 个域(domain/accounts/chat/audit/expert/notifications)的
13 张表聚合到同一 Base 之下,alembic env.py 改引此处后 target_metadata
才能看到全部 13 张表(否则 alembic check 会产 DROP)。

D-1 边界:本模块是 0 业务逻辑的纯聚合,不引任何 API 路由 / use case /
service / 测试 fixture;只 import core.db(透传 Base/BaseMixin)与
5 个域的 models。

关键纠错(6.E 必须沿用):
- alembic env.py 改 `from app.core.models import Base`(不是 core.db)
- 5 域 import 一个不能漏,否则 alembic check 产 DROP

选择显式逐类 import 而非 `from X.models import *`:
- 显式 import 不触发 F403(import * 的警告)
- 类名清单与 __all__ 一一对应,IDE 跳转 / static check 更友好
- 与 6.B base.py shim 风格一致
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
