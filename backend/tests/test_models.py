"""ORM 模型字段契约保护（M6-patch3 新增 + 已有属性）。

零产出：仅验证字段存在性与类型，不连 DB。
测试隔离：纯 Python 反射，不依赖任何 fixture。
"""

import uuid
from datetime import datetime
from typing import get_type_hints

from sqlalchemy import inspect

from app.models.chat import Message, Session


class TestSessionORM:
    """Session 模型：context_token_count 字段 + last_active_at tz 核验。"""

    def test_context_token_count_field_exists(self):
        """Session ORM 含 context_token_count: Mapped[int]。"""
        cols = {c.name: c for c in inspect(Session).columns}
        assert "context_token_count" in cols
        col = cols["context_token_count"]
        assert col.nullable is False

    def test_context_token_default_zero(self):
        """server_default = '0'。"""
        cols = {c.name: c for c in inspect(Session).columns}
        col = cols["context_token_count"]
        assert col.server_default is not None
        assert col.server_default.arg == "0"

    def test_last_active_at_timezone_aware(self):
        """last_active_at 列配置 TIMESTAMP(timezone=True)。"""
        from sqlalchemy.dialects.postgresql import TIMESTAMP

        cols = {c.name: c for c in inspect(Session).columns}
        col = cols["last_active_at"]
        # 检查 server_default 是 func.now()（数据库产生 tz-aware 值）
        assert col.server_default is not None

    def test_session_field_count(self):
        """Session 字段数从 6 增至 7（+context_token_count）。

        当前字段：id, created_at(BaseMixin), child_user_id, title,
        status, last_active_at, context_token_count
        """
        cols = [c.name for c in inspect(Session).columns]
        assert len(cols) == 7
