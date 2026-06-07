"""集成测试基建冒烟测试（M9.5 Step 2–6）。

确认真 DB / 真 Redis / 真 RuntimeResources / arq worker 可用。
"""
from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class _SmokeAuditLLM:
    """极简 FakeLLM，验证 arq worker enqueue→drain 机械通断 + 审计图写库。

    返回带 AuditOutputSchema tool_call 的 AIMessage，使 run_audit 正常
    完成 write_results（包括 audit_records INSERT + rolling_summaries upsert）。
    """
    async def ainvoke(self, messages, **kwargs):
        from tests.integration.chat._helpers import make_audit_tool_call
        return AIMessage(content="", tool_calls=make_audit_tool_call(
            turn_summary="冒烟测试",
        ))
    async def astream(self, messages, **kwargs):
        from langchain_core.messages import AIMessageChunk
        yield AIMessageChunk(content="冒烟测试")
    def bind_tools(self, tools, **kwargs):
        return self
    def with_retry(self, **kwargs):
        return self
    def with_fallbacks(self, fallbacks, **kwargs):
        return self


class TestInfrastructureSmoke:
    """基建冒烟：DB / Redis / RuntimeResources / arq worker 贯通。"""

    async def test_db_bootstrap_and_truncate(
        self,
    ) -> None:
        """truncate_tables autouse，DB 可 bootstrap（无异常即通过）。"""
        pass

    async def test_redis_flushdb(
        self,
        integration_redis,
    ) -> None:
        """Redis fixture SET/GET 跨测试隔离。"""
        await integration_redis.set("smoke_key", "smoke_val")
        val = await integration_redis.get("smoke_key")
        assert val == "smoke_val"

    async def test_runtime_resources(
        self,
        integration_runtime,
    ) -> None:
        """RuntimeResources 含真 engine / session_factory / graphs。"""
        rr = integration_runtime
        # db_session_factory 可创建 session
        async with rr.db_session_factory() as session:
            from sqlalchemy import text
            result = await session.execute(text("SELECT 1 AS val"))
            assert result.scalar_one() == 1
        # main_graph 可编译
        assert rr.main_graph is not None
        assert rr.audit_graph is not None
        # register_chat_task 句柄可暴露
        assert hasattr(rr, "register_chat_task")
        assert hasattr(rr, "_chat_tasks")

    async def test_app_accepts_request(
        self,
        api_client,
    ) -> None:
        """App fixture 可接受 HTTP 请求（health check）。"""
        resp = await api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"

    async def test_arq_worker_enqueue_drain(
        self,
        integration_runtime,
    ) -> None:
        """arq worker enqueue→drain round-trip（关注点 4 命门）。

        验证：
          1. 通过 enqueue_audit helper 入队 audit job
          2. 自建 Worker（使用字符串路径注册，与 graph.py 入队名一致）
             drain 消费到该 job（processed ≥ 1）
          3. redis_pool 与 enqueue 端同源（同一 integration_runtime.arq_pool）
          4. 审计图 write_results 正常落库（需 DB 中有 family + user + session）
        """
        from app.audit.worker import WORKER_SETTINGS
        from app.core.enums import SessionStatus, UserRole
        from app.core.llm import clear_test_llm, set_test_llm
        from app.domain.audit.signals import AuditSignalsManager
        from app.domain.chat.usecase import enqueue_audit
        from app.models.accounts import Family, User
        from app.models.chat import Session as SessionModel
        from arq import Worker

        rr = integration_runtime

        # 创建 family + user + session（write_results 需 FK 可引用）
        async with rr.db_session_factory() as db:
            fam = Family()
            db.add(fam)
            await db.flush()

            child = User(family_id=fam.id, role=UserRole.child, phone="0000", is_active=True)
            db.add(child)
            await db.flush()

            ses = SessionModel(
                id=uuid.uuid4(),
                child_user_id=child.id,
                status=SessionStatus.active,
            )
            db.add(ses)
            await db.commit()

        sid = ses.id
        turn = 1
        child_id = child.id
        msg_id = uuid.uuid4()

        # 自建 worker：使用字符串路径注册，与 enqueue_audit 入队名一致
        async def _on_startup(ctx):
            ctx["resources"] = rr
            ctx["signals_manager"] = AuditSignalsManager(
                rr.audit_redis,
                ttl=rr.settings.audit_redis_ttl_seconds,
            )

        worker = Worker(
            functions=["app.audit.worker.run_audit"],
            redis_pool=rr.arq_pool,
            burst=True,
            on_startup=_on_startup,
            max_tries=1,
            job_timeout=WORKER_SETTINGS["job_timeout"],
        )

        try:
            set_test_llm("audit_deepseek", _SmokeAuditLLM())

            async with rr.db_session_factory() as db:
                await enqueue_audit(
                    arq_pool=rr.arq_pool,
                    audit_redis=rr.audit_redis,
                    sid=sid,
                    db=db,
                    turn_number=turn,
                    child_user_id=child_id,
                    target_message_id=msg_id,
                )

            processed = await worker.run_check()
            assert processed >= 1, (
                f"arq_worker drain 应消费 ≥1 job，实际 {processed}"
            )
        finally:
            clear_test_llm()
            await worker.close()
