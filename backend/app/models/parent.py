"""Transitional shim — 6.C 临时保留,6.4 整体删除。

原 parent.py 3 张表已分散到 3 个不同域:
- DailyReport         → app.domain.expert.models
- Notification        → app.domain.notifications.models
- DataDeletionRequest → app.domain.accounts.models(D6-1)

此处从 3 个域分别再导出,保持 from app.models.parent import X 老路径可用。
"""

from app.domain.accounts.models import DataDeletionRequest
from app.domain.expert.models import DailyReport
from app.domain.notifications.models import Notification

__all__ = ["DailyReport", "DataDeletionRequest", "Notification"]
