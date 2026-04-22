"""redis_ops 单元测试：commit_with_redis 语义、discard_pending_redis_ops。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.auth.redis_ops import (
    RedisOp,
    commit_with_redis,
    discard_pending_redis_ops,
    stage_redis_op,
)


class TestRedisOpsStaging:
    @pytest.mark.asyncio
    async def test_stage_redis_op_appends_to_session_info(
        self, db_session
    ) -> None:
        op = RedisOp(kind="setex", key="test:key", ttl_seconds=60, value="val")
        stage_redis_op(db_session, op)

        pending = db_session.info.get("pending_redis_ops", [])
        assert len(pending) == 1
        assert pending[0].key == "test:key"
        discard_pending_redis_ops(db_session)  # 清理，避免 teardown 护栏误报

    @pytest.mark.asyncio
    async def test_discard_pending_redis_ops_clears_session_info(
        self, db_session
    ) -> None:
        stage_redis_op(db_session, RedisOp(kind="setex", key="k", ttl_seconds=10, value="v"))
        discard_pending_redis_ops(db_session)

        assert db_session.info.get("pending_redis_ops", []) == []

    @pytest.mark.asyncio
    async def test_commit_with_redis_happy_path(
        self, db_session, redis_client
    ) -> None:
        stage_redis_op(db_session, RedisOp(kind="setex", key="k1", ttl_seconds=60, value="v1"))
        stage_redis_op(db_session, RedisOp(kind="delete", key="k2"))

        await commit_with_redis(db_session, redis_client)

        val = await redis_client.get("k1")
        assert val == "v1"
        exists = await redis_client.exists("k2")
        assert exists == 0
        assert db_session.info.get("pending_redis_ops", []) == []

    @pytest.mark.asyncio
    async def test_commit_with_redis_no_ops_just_commits(
        self, db_session, redis_client
    ) -> None:
        # 无 ops 时直接 commit，不抛
        await commit_with_redis(db_session, redis_client)
        assert db_session.info.get("pending_redis_ops", []) == []

    @pytest.mark.asyncio
    async def test_commit_with_redis_db_error_propagates(
        self, db_session, redis_client
    ) -> None:
        stage_redis_op(db_session, RedisOp(kind="setex", key="k", ttl_seconds=60, value="v"))

        # monkeypatch db.commit to raise
        original_commit = db_session.commit
        db_session.commit = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="boom"):
            await commit_with_redis(db_session, redis_client)

        # ops 已 pop，不会在 session close 时触发 teardown 护栏
        assert db_session.info.get("pending_redis_ops", []) == []

        db_session.commit = original_commit  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_commit_with_redis_redis_error_does_not_propagate(
        self, db_session, redis_client
    ) -> None:
        stage_redis_op(db_session, RedisOp(kind="setex", key="k", ttl_seconds=60, value="v"))

        # monkeypatch pipeline to return a MagicMock (not AsyncMock) so __aenter__
        # returns the pipe synchronously, avoiding 'coroutine never awaited' warning.
        original_pipeline = redis_client.pipeline

        mock_pipe = MagicMock()
        mock_pipe.__aenter__.return_value = mock_pipe
        mock_pipe.__aexit__.return_value = None

        async def _raise(*args, **kwargs):
            raise RuntimeError("redis boom")

        mock_pipe.execute = _raise
        redis_client.pipeline = MagicMock(return_value=mock_pipe)  # type: ignore[method-assign]

        # 不抛异常
        await commit_with_redis(db_session, redis_client)

        redis_client.pipeline = original_pipeline  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_discard_clears_without_commit(
        self, db_session, redis_client
    ) -> None:
        stage_redis_op(
            db_session, RedisOp(kind="setex", key="discarded", ttl_seconds=60, value="v")
        )
        discard_pending_redis_ops(db_session)

        # session rollback 时不会触发 teardown 护栏
        assert db_session.info.get("pending_redis_ops", []) == []
