"""Step 7 TDD: GET /me/sessions · GET /me/sessions/{id}/messages · DELETE /me/sessions/{id}."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import User
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from app.models.enums import MessageRole


async def _login(api_client, user: User, pw: str, device_id: str = "test_device") -> str:
    login_resp = await api_client.post(
        "/api/v1/auth/login",
        json={"phone": user.phone, "password": pw, "device_id": device_id},
    )
    return login_resp.json()["token"]


async def _bind_child(api_client, parent, parent_pw, child_user_id: UUID) -> str:
    """Bind child via parent and return child token."""
    parent_token = await _login(api_client, parent, parent_pw)
    bind_resp = await api_client.post(
        "/api/v1/bind-tokens",
        json={"child_user_id": str(child_user_id)},
        headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
    )
    assert bind_resp.status_code == 201, f"bind failed: {bind_resp.json()}"
    bind_token = bind_resp.json()["bind_token"]
    redeem_resp = await api_client.post(
        f"/api/v1/bind-tokens/{bind_token}/redeem",
        json={"device_id": "child_device"},
    )
    assert redeem_resp.status_code == 200, f"redeem failed: {redeem_resp.json()}"
    return redeem_resp.json()["token"]


async def _child_with_token_via_api(
    api_client, db_session: AsyncSession, seeded_parent: tuple[User, str], phone: str = "ch01"
) -> tuple[UUID, str]:
    """Create child via POST /children API + bind, return (child_id, child_token)."""
    parent, pw = seeded_parent
    parent_token = await _login(api_client, parent, pw)

    # Create child via API (includes ChildProfile)
    child_resp = await api_client.post(
        "/api/v1/children",
        json={"nickname": "TestChild", "age": 10, "gender": "unknown"},
        headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
    )
    assert child_resp.status_code == 201, f"create child failed: {child_resp.json()}"
    child_id = UUID(child_resp.json()["id"])

    # Bind and redeem
    child_token = await _bind_child(api_client, parent, pw, child_id)
    return child_id, child_token


def _make_cursor(sort_key: datetime, row_id: str) -> str:
    import base64

    # Must match _encode_cursor: strip timezone to naive UTC before encoding
    if sort_key.tzinfo is not None:
        sort_key = sort_key.replace(tzinfo=None)
    return base64.urlsafe_b64encode(f"{sort_key.isoformat()}|{row_id}".encode()).decode()


# ---------------------------------------------------------------------------
# GET /me/sessions
# ---------------------------------------------------------------------------


class TestListSessionsHappy:
    """Happy path: returns sessions ordered by last_active_at DESC."""

    @pytest.mark.asyncio
    async def test_returns_sessions_for_child(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Given child has 2 sessions, returns them ordered by last_active_at DESC."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent
        )

        # Create sessions directly in db
        s1 = SessionModel(
            child_user_id=child_id,
            title="Session 1",
            status="active",
            last_active_at=datetime(2025, 1, 2, 12, 0, 0, tzinfo=timezone.utc),
        )
        s2 = SessionModel(
            child_user_id=child_id,
            title="Session 2",
            status="active",
            last_active_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        db_session.add_all([s1, s2])
        await db_session.commit()

        resp = await api_client.get(
            "/api/v1/me/sessions",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert len(data["items"]) == 2
        # Ordered by last_active_at DESC
        assert data["items"][0]["title"] == "Session 1"
        assert data["items"][1]["title"] == "Session 2"
        assert data["next_cursor"] is None

    @pytest.mark.asyncio
    async def test_default_limit_15(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """No limit param → defaults to 15."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch02"
        )

        for i in range(3):
            db_session.add(SessionModel(child_user_id=child_id, title=f"S{i}", status="active"))
        await db_session.commit()

        resp = await api_client.get(
            "/api/v1/me/sessions",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 3

    @pytest.mark.asyncio
    async def test_limit_50_ok(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """limit=50 → 200."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch03"
        )
        db_session.add(SessionModel(child_user_id=child_id, title="S1", status="active"))
        await db_session.commit()

        resp = await api_client.get(
            "/api/v1/me/sessions?limit=50",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_limit_51_returns_422(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """limit > 50 → 422."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch04"
        )
        db_session.add(SessionModel(child_user_id=child_id, title="S1", status="active"))
        await db_session.commit()

        resp = await api_client.get(
            "/api/v1/me/sessions?limit=51",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 422


class TestListSessionsKeysetPagination:
    """Keyset cursor pagination."""

    @pytest.mark.asyncio
    async def test_pagination_next_cursor(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """First request returns next_cursor; second request with cursor returns earlier page."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch10"
        )

        # Create 3 sessions with distinct timestamps
        for i in range(3):
            db_session.add(
                SessionModel(
                    child_user_id=child_id,
                    title=f"S{i}",
                    status="active",
                    last_active_at=datetime(2025, 1, i + 1, 12, 0, 0, tzinfo=timezone.utc),
                )
            )
        await db_session.commit()

        hdrs = {"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"}

        # Page 1 (limit=2)
        resp1 = await api_client.get("/api/v1/me/sessions?limit=2", headers=hdrs)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert len(data1["items"]) == 2
        assert data1["next_cursor"] is not None

        # Page 2 with cursor
        resp2 = await api_client.get(
            f"/api/v1/me/sessions?limit=2&cursor={data1['next_cursor']}",
            headers=hdrs,
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert len(data2["items"]) == 1
        assert data2["next_cursor"] is None  # last page


class TestListSessionsCursorValidation:
    """Cursor decode errors → 400 InvalidCursor."""

    @pytest.mark.asyncio
    async def test_bad_base64_cursor_authed(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Bad base64 → 400 InvalidCursor."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch20"
        )
        db_session.add(SessionModel(child_user_id=child_id, title="S1", status="active"))
        await db_session.commit()

        resp = await api_client.get(
            "/api/v1/me/sessions?cursor=NOT_VALID_BASE64!!!",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 400
        assert "InvalidCursor" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_bad_iso_cursor_authed(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Valid base64 but invalid ISO timestamp → 400 InvalidCursor."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch21"
        )
        db_session.add(SessionModel(child_user_id=child_id, title="S1", status="active"))
        await db_session.commit()

        import base64

        bad_cursor = base64.urlsafe_b64encode("not-a-date|not-a-uuid".encode()).decode()
        resp = await api_client.get(
            f"/api/v1/me/sessions?cursor={bad_cursor}",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 400
        assert "InvalidCursor" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_bad_uuid_in_cursor_authed(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Valid ISO but invalid UUID → 400 InvalidCursor."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch22"
        )
        db_session.add(SessionModel(child_user_id=child_id, title="S1", status="active"))
        await db_session.commit()

        import base64

        bad_cursor = base64.urlsafe_b64encode("2025-01-01T12:00:00|not-a-uuid".encode()).decode()
        resp = await api_client.get(
            f"/api/v1/me/sessions?cursor={bad_cursor}",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 400
        assert "InvalidCursor" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_empty_string_cursor_returns_first_page(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Empty string cursor → treated as no cursor (first page)."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch23"
        )
        db_session.add(SessionModel(child_user_id=child_id, title="S1", status="active"))
        await db_session.commit()

        resp = await api_client.get(
            "/api/v1/me/sessions?cursor=",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

    @pytest.mark.asyncio
    async def test_cursor_cross_account_protection(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Cursor with other child's session → returns empty items (WHERE child_user_id guard)."""
        parent, pw = seeded_parent

        # Create two children
        child1_id, child1_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch24a"
        )
        child2_id, child2_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch24b"
        )

        s1 = SessionModel(
            child_user_id=child1_id,
            title="Child1 S1",
            status="active",
            last_active_at=datetime(2025, 1, 2, 12, 0, 0, tzinfo=timezone.utc),
        )
        s2 = SessionModel(
            child_user_id=child2_id,
            title="Child2 S1",
            status="active",
            last_active_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        db_session.add_all([s1, s2])
        await db_session.commit()

        hdrs = {"Authorization": f"Bearer {child1_token}", "X-Device-Id": "child_device"}

        # child1 lists → only sees own session
        resp = await api_client.get("/api/v1/me/sessions", headers=hdrs)
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1
        assert resp.json()["items"][0]["title"] == "Child1 S1"

        # Encode child2's session cursor and use it as child1 — WHERE child_user_id protects
        cursor = _make_cursor(s2.last_active_at, str(s2.id))
        resp2 = await api_client.get(f"/api/v1/me/sessions?cursor={cursor}", headers=hdrs)
        # child1 can't see child2's session — returns empty (WHERE child_user_id=child1)
        assert resp2.status_code == 200
        assert len(resp2.json()["items"]) == 0
        assert resp2.json()["next_cursor"] is None


class TestListSessionsStatus:
    """Only status='active' sessions are returned; discarded/deleted are filtered."""

    @pytest.mark.asyncio
    async def test_discarded_session_not_returned(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """status='deleted' session → not in list."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch30"
        )

        db_session.add(SessionModel(child_user_id=child_id, title="Active", status="active"))
        db_session.add(SessionModel(child_user_id=child_id, title="Deleted", status="deleted"))
        await db_session.commit()

        resp = await api_client.get(
            "/api/v1/me/sessions",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 200
        assert [i["title"] for i in resp.json()["items"]] == ["Active"]


# ---------------------------------------------------------------------------
# GET /me/sessions/{id}/messages
# ---------------------------------------------------------------------------


class TestGetMessagesHappy:
    """Happy path for GET /me/sessions/{id}/messages."""

    @pytest.mark.asyncio
    async def test_returns_messages_newest_first(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Messages returned in reverse-chronological order (created_at DESC)."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch40"
        )

        s = SessionModel(child_user_id=child_id, title="Test", status="active")
        db_session.add(s)
        await db_session.flush()

        m1 = Message(session_id=s.id, role=MessageRole.human, content="First", status="active")
        m2 = Message(session_id=s.id, role=MessageRole.ai, content="Second", status="active")
        db_session.add_all([m1, m2])
        await db_session.flush()
        # m1 older, m2 newer
        m1.created_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        m2.created_at = datetime(2025, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        await db_session.commit()

        resp = await api_client.get(
            f"/api/v1/me/sessions/{s.id}/messages",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        # Newest first
        assert data["items"][0]["content"] == "Second"
        assert data["items"][1]["content"] == "First"
        assert data["in_progress"] is False
        assert data["next_cursor"] is None

    @pytest.mark.asyncio
    async def test_status_discarded_filtered(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """status='discarded' messages are not returned."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch41"
        )

        s = SessionModel(child_user_id=child_id, title="Test", status="active")
        db_session.add(s)
        await db_session.flush()

        db_session.add(
            Message(session_id=s.id, role=MessageRole.human, content="Active", status="active")
        )
        db_session.add(
            Message(
                session_id=s.id, role=MessageRole.human, content="Discarded", status="discarded"
            )
        )
        await db_session.commit()

        resp = await api_client.get(
            f"/api/v1/me/sessions/{s.id}/messages",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 200
        contents = [m["content"] for m in resp.json()["items"]]
        assert "Discarded" not in contents
        assert "Active" in contents

    @pytest.mark.asyncio
    async def test_in_progress_true_when_lock_exists(
        self,
        api_client,
        db_session: AsyncSession,
        redis_client,
        seeded_parent: tuple[User, str],
    ) -> None:
        """When Redis lock exists, in_progress is True."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch42"
        )

        s = SessionModel(child_user_id=child_id, title="Test", status="active")
        db_session.add(s)
        await db_session.commit()

        # Set Redis lock
        await redis_client.set(f"chat:lock:{s.id}", "nonce_value", px=180_000)

        resp = await api_client.get(
            f"/api/v1/me/sessions/{s.id}/messages",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 200
        assert resp.json()["in_progress"] is True

    @pytest.mark.asyncio
    async def test_in_progress_false_when_no_lock(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """When no Redis lock, in_progress is False."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch43"
        )

        s = SessionModel(child_user_id=child_id, title="Test", status="active")
        db_session.add(s)
        await db_session.commit()

        resp = await api_client.get(
            f"/api/v1/me/sessions/{s.id}/messages",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 200
        assert resp.json()["in_progress"] is False


class TestGetMessagesAuth:
    """Auth: 401 / 403 / 404."""

    @pytest.mark.asyncio
    async def test_other_child_session_403(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Child accessing another child's session → 403."""
        parent, pw = seeded_parent
        child1_id, child1_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch50a"
        )
        child2_id, child2_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch50b"
        )

        s = SessionModel(child_user_id=child2_id, title="Child2 Session", status="active")
        db_session.add(s)
        await db_session.commit()

        resp = await api_client.get(
            f"/api/v1/me/sessions/{s.id}/messages",
            headers={"Authorization": f"Bearer {child1_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_nonexistent_session_404(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Non-existent session ID → 404."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch51"
        )
        # No sessions created

        resp = await api_client.get(
            f"/api/v1/me/sessions/{uuid4()}/messages",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 404


class TestGetMessagesCursorPagination:
    """Keyset cursor pagination for messages."""

    @pytest.mark.asyncio
    async def test_messages_keyset_pagination(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """First page → next_cursor; second page with cursor → earlier messages."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch60"
        )

        s = SessionModel(child_user_id=child_id, title="Test", status="active")
        db_session.add(s)
        await db_session.flush()

        for i in range(3):
            m = Message(session_id=s.id, role=MessageRole.human, content=f"M{i}", status="active")
            db_session.add(m)
        await db_session.commit()

        hdrs = {"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"}

        # Page 1 (limit=2)
        resp1 = await api_client.get(f"/api/v1/me/sessions/{s.id}/messages?limit=2", headers=hdrs)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert len(data1["items"]) == 2
        assert data1["next_cursor"] is not None

        # Page 2 with cursor
        resp2 = await api_client.get(
            f"/api/v1/me/sessions/{s.id}/messages?limit=2&cursor={data1['next_cursor']}",
            headers=hdrs,
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert len(data2["items"]) == 1
        assert data2["next_cursor"] is None


# ---------------------------------------------------------------------------
# DELETE /me/sessions/{id}
# ---------------------------------------------------------------------------


class TestDeleteSession:
    """Soft-delete session (status='deleted')."""

    @pytest.mark.asyncio
    async def test_delete_returns_204(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """First DELETE → 204."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch70"
        )

        s = SessionModel(child_user_id=child_id, title="To Delete", status="active")
        db_session.add(s)
        await db_session.commit()

        resp = await api_client.delete(
            f"/api/v1/me/sessions/{s.id}",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_second_delete_returns_404(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Second DELETE (already deleted) → 404."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch71"
        )

        s = SessionModel(child_user_id=child_id, title="To Delete", status="active")
        db_session.add(s)
        await db_session.commit()

        hdrs = {"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"}

        resp1 = await api_client.delete(f"/api/v1/me/sessions/{s.id}", headers=hdrs)
        assert resp1.status_code == 204

        resp2 = await api_client.delete(f"/api/v1/me/sessions/{s.id}", headers=hdrs)
        assert resp2.status_code == 404
        assert "SessionNotFound" in resp2.json()["detail"]

    @pytest.mark.asyncio
    async def test_delete_then_get_messages_404(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """After DELETE, GET messages → 404 (not empty array)."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch72"
        )

        s = SessionModel(child_user_id=child_id, title="Test", status="active")
        db_session.add(s)
        await db_session.flush()
        db_session.add(
            Message(session_id=s.id, role=MessageRole.human, content="Hi", status="active")
        )
        await db_session.commit()

        hdrs = {"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"}

        await api_client.delete(f"/api/v1/me/sessions/{s.id}", headers=hdrs)

        resp = await api_client.get(f"/api/v1/me/sessions/{s.id}/messages", headers=hdrs)
        # Session status check catches deleted session → 404
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_other_child_delete_403(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """Child deleting another child's session → 403."""
        parent, pw = seeded_parent
        child1_id, child1_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch73a"
        )
        child2_id, child2_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch73b"
        )

        s = SessionModel(child_user_id=child2_id, title="Child2 Session", status="active")
        db_session.add(s)
        await db_session.commit()

        resp = await api_client.delete(
            f"/api/v1/me/sessions/{s.id}",
            headers={"Authorization": f"Bearer {child1_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_nonexistent_session_delete_404(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        """DELETE non-existent session → 404."""
        parent, pw = seeded_parent
        child_id, child_token = await _child_with_token_via_api(
            api_client, db_session, seeded_parent, phone="ch74"
        )

        resp = await api_client.delete(
            f"/api/v1/me/sessions/{uuid4()}",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 404
