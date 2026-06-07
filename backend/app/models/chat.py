"""Transitional shim — 6.C 临时保留,6.4 整体删除。

2 张表(Session / Message)已迁至 app.domain.chat.models。
"""

from app.domain.chat.models import Message, Session

__all__ = ["Message", "Session"]
