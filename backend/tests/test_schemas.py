"""Schema 单元测试：SessionListResponse 字段 / today_session_id 类型 / 重命名。"""

from datetime import datetime, timezone
import uuid

from app.schemas.sessions import SessionListResponse, SessionListItem


class TestSessionListResponse:
    """M6-patch3 新增字段与重命名契约。"""

    def test_sessions_field_replaces_items(self):
        """'sessions' 取代 'items'，且 'items' 不存在。"""
        fields = SessionListResponse.model_fields
        assert "sessions" in fields
        assert "items" not in fields

    def test_today_session_id_type(self):
        """today_session_id 类型为 UUID | None。"""
        ann = SessionListResponse.model_fields["today_session_id"].annotation
        # Python 3.14: types.UnionType shows both UUID and None
        import typing
        args = typing.get_args(ann)
        assert uuid.UUID in args
        assert type(None) in args

    def test_today_session_id_default_none(self):
        """today_session_id 默认值为 None。"""
        resp = SessionListResponse(
            sessions=[],
            next_cursor=None,
        )
        assert resp.today_session_id is None

    def test_build_with_today_session_id(self):
        """构造时指定 today_session_id 正确保留。"""
        sid = uuid.uuid4()
        resp = SessionListResponse(
            sessions=[
                SessionListItem(id=uuid.uuid4(), title="test", last_active_at=datetime(2026, 5, 11, tzinfo=timezone.utc)),
            ],
            today_session_id=sid,
            next_cursor=None,
        )
        assert resp.today_session_id == sid
