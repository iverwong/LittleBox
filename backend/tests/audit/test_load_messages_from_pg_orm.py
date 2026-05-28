"""_load_messages_from_pg ORM 路径回归（T16 C1 / H1 D-patch0-11）。

测试 ORM 改造零行为漂移：
1. 不过滤 status（返回全部 seed 消息，含非 active）
2. createdAt ASC 顺序（DESC LIMIT + Python reversed() 翻转）
3. limit 生效（超过 limit 的行被截断）

测试隔离铁律：db_session fixture（M5），禁止 self-engine / raw SQL。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.audit.graph import _load_messages_from_pg
from app.models.chat import Message
from app.models.enums import MessageRole, MessageStatus


@pytest.mark.asyncio
async def test_load_messages_from_pg_no_status_filter(db_session):
    """ORM 改造不过滤 status：混入非 active 行仍全部返回（C1 D-patch0-11）。"""
    import uuid

    from datetime import datetime, timezone

    from app.models.accounts import Family, User
    from app.models.chat import Session as SessionModel
    from app.models.enums import UserRole

    # 创建 FK 链：user → session → messages
    fam = Family()
    db_session.add(fam)
    await db_session.flush()
    user = User(family_id=fam.id, role=UserRole.child, phone="orm1", is_active=True)
    db_session.add(user)
    await db_session.flush()

    sid = uuid.uuid4()
    db_session.add(SessionModel(id=sid, child_user_id=user.id, status="active"))
    await db_session.flush()

    # Seed 10 条消息：active×7 + discarded×2 + compressed×1
    # 验证 _load_messages_from_pg 不过滤 status（D-patch0-11 契约保留）
    seeds = []
    for i in range(7):
        seeds.append(Message(
            session_id=sid, role=MessageRole.human,
            content=f"active msg {i}", status=MessageStatus.active,
            created_at=datetime(2025, 1, 1, i, 0, 0, tzinfo=timezone.utc),
        ))
    for i in range(2):
        seeds.append(Message(
            session_id=sid, role=MessageRole.ai,
            content=f"discarded msg {i}", status=MessageStatus.discarded,
            created_at=datetime(2025, 1, 1, 7 + i, 0, 0, tzinfo=timezone.utc),
        ))
    seeds.append(Message(
        session_id=sid, role=MessageRole.ai,
        content="compressed msg", status=MessageStatus.compressed,
        created_at=datetime(2025, 1, 1, 9, 0, 0, tzinfo=timezone.utc),
    ))
    for m in seeds:
        db_session.add(m)
    await db_session.flush()
    for m in seeds:
        await db_session.refresh(m)
    await db_session.commit()

    # C7：fake factory —— 闭包捕获 db_session
    @asynccontextmanager
    async def _fake_factory():
        yield db_session

    result = await _load_messages_from_pg(str(sid), _fake_factory, limit=10)

    # 返回全部 10 条（不过滤 status）
    assert len(result) == 10, f"应返回全部 10 条，实际 {len(result)}"

    # createdAt ASC 顺序（reversed(rows) 翻转为正序）
    assert "active msg 0" in result[0].content, "首条应为最早消息"
    assert "compressed msg" in result[-1].content, "末条应为最晚消息"

    # content 验证确保完整保留
    contents = [m.content for m in result]
    assert all(c in contents for c in ["active msg 1", "discarded msg 0", "compressed msg"])


@pytest.mark.asyncio
async def test_load_messages_from_pg_limit_works(db_session):
    """limit 生效：超限行被截断。"""
    import uuid

    from datetime import datetime, timezone

    from app.models.accounts import Family, User
    from app.models.chat import Session as SessionModel
    from app.models.enums import UserRole

    fam = Family()
    db_session.add(fam)
    await db_session.flush()
    user = User(family_id=fam.id, role=UserRole.child, phone="orm2", is_active=True)
    db_session.add(user)
    await db_session.flush()

    sid = uuid.uuid4()
    db_session.add(SessionModel(id=sid, child_user_id=user.id, status="active"))
    await db_session.flush()
    for i in range(12):
        db_session.add(Message(
            session_id=sid, role=MessageRole.human,
            content=f"msg {i}", status=MessageStatus.active,
            created_at=datetime(2025, 1, 1, i, 0, 0, tzinfo=timezone.utc),
        ))
    await db_session.commit()

    @asynccontextmanager
    async def _fake_factory():
        yield db_session

    # limit=5 只返回最近 5 条（DESC LIMIT 5 + reversed 翻正序）
    result = await _load_messages_from_pg(str(sid), _fake_factory, limit=5)
    assert len(result) == 5, f"limit=5 应只返回 5 条，实际 {len(result)}"
    # 应为 msg 7–11（0-indexed：最早 0-6 被截断）
    assert "msg 7" in result[0].content, "首条应为 msg 7"
    assert "msg 11" in result[-1].content, "末条应为 msg 11"
