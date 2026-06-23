"""审查 Worker 测试：run_audit 全路径 + 失败标记语义（D14）。

覆盖 4 场景：
1. happy path → set_ready + 不抛异常
2. 最后尝试失败（job_try=3）→ set_failed + 抛异常
3. 前几次失败（job_try=1）→ set_failed 不调 + 抛异常
4. WorkerSettings 结构正确性

T10（D-patch0-7）：mock RuntimeResources 替代 build_audit_graph 直接 mock。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.worker import MAX_TRIES
from app.domain.audit.worker import run_audit
from app.core.runtime import RuntimeResources
from app.domain.audit.schemas import AuditDimensionScores, AuditOutputSchema

pytestmark = pytest.mark.audit

SID = "00000000-0000-0000-0000-000000000001"
CUID = "00000000-0000-0000-0000-000000000002"
TARGET_MID = "00000000-0000-0000-0000-000000000003"


def _fake_child_profile_dict() -> dict:
    """构造 run_audit 接收的 child_profile 字典（R2 入参 asdict 序列化）。"""
    from datetime import date

    return {
        "child_user_id": "00000000-0000-0000-0000-000000000002",
        "nickname": "test_kid",
        "gender": "unknown",
        "birth_date": date(2013, 1, 1),
        "age": 12,
        "sensitivity": None,
        "custom_redlines": None,
        "concerns": None,
    }


_AUDIT_OUTPUT = AuditOutputSchema(
    dimension_scores=AuditDimensionScores(),
    crisis_detected=False,
    crisis_topic=None,
    redline_triggered=False,
    redline_detail=None,
    guidance_injection="ok",
    turn_summary="ok",
)


async def _fake_graph_ainvoke_ok(state: dict, **kwargs: object) -> dict:
    """模拟 graph 返回含有 structured_output 的结果。"""
    return {"structured_output": _AUDIT_OUTPUT}


async def _fake_graph_ainvoke_raise(state: dict, **kwargs: object) -> dict:
    """模拟 graph 抛异常。"""
    msg = f"LLM error sid={state.get('sid', '?')} turn={state.get('turn_number', '?')}"
    raise RuntimeError(msg)


def _make_fake_rr() -> MagicMock:
    """构造 fake RuntimeResources 供 worker 测试使用。

    T10 后 run_audit 不直接调 build_audit_graph，而是从 ctx["resources"]
    取 rr.audit_graph.ainvoke()。测试通过此工厂构造 mock RuntimeResources，
    注入预期的 ainvoke 行为。
    """
    fake_rr = MagicMock(spec=RuntimeResources)
    fake_rr.audit_graph = AsyncMock()
    fake_rr.audit_graph.ainvoke = AsyncMock()
    fake_rr.settings = MagicMock()
    fake_rr.settings.max_audit_tool_iterations = 5
    fake_rr.db_session_factory = MagicMock()
    fake_rr.audit_redis = MagicMock()
    fake_rr.shared_http_client = MagicMock()
    return fake_rr


def _make_ctx(
    *,
    job_try: int = 1,
    sid: str = SID,
    turn: int = 1,
) -> dict:
    """构造模拟 ARQ ctx，含 RuntimeResources + signals_manager。"""
    from app.domain.audit.signals import AuditSignalsManager
    from fakeredis.aioredis import FakeRedis

    redis = FakeRedis(decode_responses=True)
    fake_rr = _make_fake_rr()
    fake_rr.audit_redis = redis

    return {
        "job_id": f"audit:{sid}:{turn}",
        "job_try": job_try,
        "redis": redis,
        "resources": fake_rr,
        "signals_manager": AuditSignalsManager(redis, ttl=86400),
    }


class TestRunAudit:
    """run_audit job 4 场景。"""

    @pytest.mark.asyncio
    async def test_happy_path_sets_ready(self) -> None:
        """graph 成功 → set_ready 被调 + 状态为 ready。"""
        ctx = _make_ctx()
        mgr = ctx["signals_manager"]

        # 配置 fake RuntimeResources 的 audit_graph.ainvoke 返回成功结果
        fake_rr: MagicMock = ctx["resources"]
        fake_rr.audit_graph.ainvoke = _fake_graph_ainvoke_ok

        await run_audit(
            ctx,
            SID,
            turn_number=1,
            child_user_id=CUID,
            target_message_id=TARGET_MID,
            child_profile=_fake_child_profile_dict(),
        )

        # 验证 Redis 状态
        payload = await mgr.get(SID)
        assert payload is not None
        assert payload.status == "ready"

    @pytest.mark.asyncio
    async def test_final_try_sets_failed(self) -> None:
        """job_try=3（max_tries）时 graph 抛异常 → set_failed + 异常上抛。"""
        ctx = _make_ctx(job_try=MAX_TRIES)
        mgr = ctx["signals_manager"]

        fake_rr: MagicMock = ctx["resources"]
        fake_rr.audit_graph.ainvoke = _fake_graph_ainvoke_raise

        with pytest.raises(RuntimeError, match="LLM error"):
            await run_audit(
                ctx,
                SID,
                turn_number=2,
                child_user_id=CUID,
                target_message_id=TARGET_MID,
                child_profile=_fake_child_profile_dict(),
            )

        # 验证 Redis 状态
        payload = await mgr.get(SID)
        assert payload is not None
        assert payload.status == "failed"
        assert payload.error is not None

    @pytest.mark.asyncio
    async def test_early_try_does_not_set_failed(self) -> None:
        """job_try=1 时 graph 抛异常 → set_failed 不调 + 异常上抛。"""
        ctx = _make_ctx(job_try=1)
        mgr = ctx["signals_manager"]

        fake_rr: MagicMock = ctx["resources"]
        fake_rr.audit_graph.ainvoke = _fake_graph_ainvoke_raise

        with pytest.raises(RuntimeError, match="LLM error"):
            await run_audit(
                ctx,
                SID,
                turn_number=2,
                child_user_id=CUID,
                target_message_id=TARGET_MID,
                child_profile=_fake_child_profile_dict(),
            )

        # 验证 Redis 状态未写入（key 不存在）
        payload = await mgr.get(SID)
        assert payload is None

    # ---- A4 worker-seam: target_message_id 透传验证 ----

    @pytest.mark.asyncio
    async def test_target_message_id_passed_to_graph_context(self) -> None:
        """Given target_message_id str, When run_audit, Then AuditContextSchema.target_message_id
        传入 audit_graph.ainvoke(context=...)，匹配 uuid.UUID 入参。

        Given/When/Then: 给定 target_message_id → run_audit 将其传给图上下文。
        """
        import uuid
        from unittest.mock import AsyncMock

        expected_tid = uuid.UUID(TARGET_MID)
        ctx = _make_ctx()
        ainvoke_spy = AsyncMock(return_value={"structured_output": _AUDIT_OUTPUT})
        fake_rr: MagicMock = ctx["resources"]
        fake_rr.audit_graph.ainvoke = ainvoke_spy

        await run_audit(
            ctx,
            SID,
            turn_number=1,
            child_user_id=CUID,
            target_message_id=TARGET_MID,
            child_profile=_fake_child_profile_dict(),
        )

        ainvoke_spy.assert_called_once()
        _, kwargs = ainvoke_spy.call_args
        audit_ctx = kwargs["context"]
        assert audit_ctx.target_message_id == expected_tid, (
            f"Expected {expected_tid}, got {audit_ctx.target_message_id}"
        )


class TestWorkerSettings:
    """WorkerSettings 结构正确性。"""

    def test_worker_settings_has_required_keys(self) -> None:
        from app.worker import WORKER_SETTINGS

        assert "functions" in WORKER_SETTINGS
        assert "redis_settings" in WORKER_SETTINGS
        assert WORKER_SETTINGS["max_tries"] == MAX_TRIES
        assert WORKER_SETTINGS["job_timeout"] == 3600
        assert WORKER_SETTINGS["on_startup"] is not None
        assert WORKER_SETTINGS["on_shutdown"] is not None
        assert WORKER_SETTINGS["on_job_start"] is not None
        assert WORKER_SETTINGS["on_job_end"] is not None
