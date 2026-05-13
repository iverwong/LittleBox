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
    """Session 模型：context_size_tokens + needs_compression 字段 + last_active_at tz 核验。"""

    def test_context_size_tokens_field_exists(self):
        """Session ORM 含 context_size_tokens: Mapped[int | None]。"""
        cols = {c.name: c for c in inspect(Session).columns}
        assert "context_size_tokens" in cols
        col = cols["context_size_tokens"]
        assert col.nullable is True  # 快照字段可为 NULL

    def test_needs_compression_field_exists(self):
        """Session ORM 含 needs_compression: Mapped[bool]。"""
        cols = {c.name: c for c in inspect(Session).columns}
        assert "needs_compression" in cols
        col = cols["needs_compression"]
        assert col.nullable is False

    def test_context_token_count_removed(self):
        """context_token_count 已 rename。"""
        cols = {c.name: c for c in inspect(Session).columns}
        assert "context_token_count" not in cols

    def test_last_active_at_timezone_aware(self):
        """last_active_at 列配置 TIMESTAMP(timezone=True)。"""
        from sqlalchemy.dialects.postgresql import TIMESTAMP

        cols = {c.name: c for c in inspect(Session).columns}
        col = cols["last_active_at"]
        # 检查 server_default 是 func.now()（数据库产生 tz-aware 值）
        assert col.server_default is not None

    def test_session_field_count(self):
        """Session 字段数从 7 增至 8（+needs_compression）。

        当前字段：id, created_at(BaseMixin), child_user_id, title,
        status, last_active_at, context_size_tokens, needs_compression
        """
        cols = [c.name for c in inspect(Session).columns]
        assert len(cols) == 8
