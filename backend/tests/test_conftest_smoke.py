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


class TestProdDbGuard:
    """_prod_db_row_count_guard session autouse fixture 自检。"""

    async def test_guard_pass_without_pollution(self, db_session):
        """Given: 正常经 db_session fixture 操作测试库
        When: session 结束
        Then: guard 不报错（测试库写入不污染真库 baseline）
        """
        from app.models.accounts import Family

        db_session.add(Family())
        await db_session.commit()
        count = await db_session.scalar(text("SELECT count(*) FROM families"))
        assert count == 1

    # test_guard_skip_with_env_var: 手动验证
    #   LB_SKIP_PROD_GUARD=1 pytest tests/ -x 应全量通过，guard 不比对。
    #   本文件不包含该用例（需要 env var 前置条件）。
