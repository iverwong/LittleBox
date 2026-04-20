"""conftest 基础设施自检；确认后可删或保留。"""
import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def test_db_session_round_trip(db_session):
    """能建表入数，commit 足够企业用但不落盘。"""
    from app.models.accounts import Family

    fam = Family()
    db_session.add(fam)
    await db_session.commit()  # 实际 release savepoint
    got = await db_session.scalar(text("SELECT count(*) FROM families"))
    assert got == 1


async def test_db_isolation_across_tests_1(db_session):
    from app.models.accounts import Family

    db_session.add(Family())
    await db_session.commit()


async def test_db_isolation_across_tests_2(db_session):
    """上个测试写入的 Family 应该已被 rollback，这里 count 必须为 0。"""
    count = await db_session.scalar(text("SELECT count(*) FROM families"))
    assert count == 0


async def test_redis_round_trip(redis_client):
    await redis_client.setex("foo", 60, "bar")
    assert await redis_client.get("foo") == "bar"


async def test_api_client_health(api_client):
    r = await api_client.get("/health")
    assert r.status_code == 200
