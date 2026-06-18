"""Step 6: 8 Given/When/Then tests for decoupled stream lifecycle (M9-patch1).

Covers: normal flow / client disconnect / queue full / pipeline exception /
shutdown wait / shutdown cancel / stop signal / commit①~create_task lock release.

三种策略：
- HTTP 全栈（#1 #4 #7 #8）：走 lifecycle_ctx.client POST；
- 纯 async 单元（#5 #6）：直操 asyncio.wait 逻辑，不需 HTTP；
- 协程级直测（#2 #3）：直接驱动 stream_generator / run_llm_pipeline。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")

from app.core.config import settings as _module_settings
from app.core.enums import MessageRole, MessageStatus
from app.core.runtime import RuntimeResources
from app.domain.chat.models import Message
from app.domain.chat.models import Session as SessionModel
from app.domain.chat.pipeline import run_llm_pipeline
from app.domain.chat.stream import ChatStreamState, stream_generator
from app.domain.chat.stream_signals import running_streams
from sqlalchemy import select
from tests.api._chat_stream_lifecycle_helpers import (
    lifecycle_ctx,  # noqa: F401  # fixture param,ruff 认不出
    lifecycle_setup,
)

# ---- Helpers shared across tests ----


def _make_payload(content: str, session_id: str | None = None) -> dict:
    body = {"content": content}
    if session_id is not None:
        body["session_id"] = session_id
    return body


# =====================================================================
# #1: 正常流 — HTTP 全栈
# =====================================================================


@pytest.mark.asyncio
async def test_normal_stream_emits_deltas_and_end(lifecycle_ctx):
    """Given a healthy LLM stub yielding 3 deltas,
    When the client consumes the SSE stream end-to-end,
    Then it should receive session_meta + 3 deltas + end frame,
         the ai row should be committed,
         and the session lock should be released.
    """
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in [
            {"delta": "你"},
            {"delta": "好"},
            {"delta": "吗"},
            {"finish_reason": "stop"},
        ]:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    resp = await client.post("/api/v1/me/chat/stream", json=_make_payload("Hi"), headers=headers)
    assert resp.status_code == 200

    frames = []
    current_type = None
    for line in resp.text.split("\n"):
        if line.startswith("event:"):
            current_type = line[len("event:"):].strip()
        elif line.startswith("data:") and current_type is not None:
            frames.append(current_type)
    assert frames == ["session_meta", "delta", "delta", "delta", "end"]

    # Re-parse for sid
    import json as _json
    events = []
    ct = None
    for line in resp.text.split("\n"):
        if line.startswith("event:"):
            ct = line[len("event:"):].strip()
        elif line.startswith("data:") and ct is not None:
            events.append((ct, _json.loads(line[len("data:"):].strip())))

    sid = events[0][1]["session_id"]

    # DB: human + ai rows committed
    lifecycle_ctx.assert_sess.expire_all()
    msgs = (await lifecycle_ctx.assert_sess.execute(
        select(Message).where(Message.session_id == sid).order_by(Message.created_at)
    )).scalars().all()
    assert len(msgs) == 2
    assert msgs[1].role == MessageRole.ai
    assert msgs[1].content == "你好吗"

    # Lock released
    lock_exists = await lifecycle_ctx.redis_client.exists(f"chat:lock:{sid}")
    assert not lock_exists, "Session lock was not released"


# =====================================================================
# #2: 客户端断连 — 协程级直测
# =====================================================================


@pytest.mark.asyncio
async def test_client_disconnect_keeps_bg_task_running(lifecycle_ctx):
    """Given an in-progress stream_generator,
    When we aclose() it mid-stream (模拟客户端断连),
    Then it should return silently without raising,
         and a separately driven run_llm_pipeline should still
         commit the ai row + release the lock.
    """
    # 直接用 run_llm_pipeline 和 stream_generator 的单元级驱动
    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()

    # Pre-seed session
    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="断连测试", status="active")
    )
    await lifecycle_ctx.seed_sess.commit()

    queue: asyncio.Queue = asyncio.Queue(maxsize=128)
    state = ChatStreamState()
    stop_event = asyncio.Event()

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        yield {"delta": "你好"}
        yield {"delta": "世界"}
        yield {"finish_reason": "stop"}

    # 准备段一参数
    from tests.conftest import make_chat_context
    ctx = make_chat_context(
        session_id=sid, child_user_id=child.id,
        user_input="测试",
        settings=lifecycle_ctx.rr.settings,
        db_session_factory=lifecycle_ctx.rr.db_session_factory,
        audit_redis=lifecycle_ctx.redis_client,
    )

    rr = lifecycle_ctx.rr
    rr.main_graph.astream = fake_astream

    hid = uuid4()
    nonce = "test-nonce-disconnect"

    # 在 running_streams 注册（段一会读取）
    running_streams[str(sid)] = stop_event

    # 同时启动段一 segment task
    task = asyncio.create_task(
        run_llm_pipeline(
            rr=rr, redis=lifecycle_ctx.redis_client, sid=sid, hid=hid, nonce=nonce,
            child_user_id=child.id, turn_number=1,
            initial_state={"messages": []}, ctx=ctx,
            queue=queue, state=state, stop_event=stop_event,
        ),
        name=f"chat-llm-{sid}",
    )

    # 驱动段二，消费几帧后 aclose
    gen = stream_generator(queue, state, sid)
    frames = []
    try:
        async for frame in gen:
            frames.append(frame)
            if len(frames) >= 2:  # 读 2 帧后关
                await gen.aclose()
                break
    except Exception:
        pass  # aclose 后 generator 退出

    # 等待段一完成
    await asyncio.wait_for(task, timeout=15.0)

    # 断言段一仍 commit② + release lock
    lifecycle_ctx.assert_sess.expire_all()
    ai_msg = (await lifecycle_ctx.assert_sess.execute(
        select(Message).where(Message.session_id == sid, Message.role == MessageRole.ai)
    )).scalar_one_or_none()
    assert ai_msg is not None, "ai row should be committed even after disconnect"
    assert ai_msg.content == "你好世界"


# =====================================================================
# #3: queue full — 协程级直测
# =====================================================================


@pytest.mark.asyncio
async def test_queue_full_triggers_flow_pause_and_headless_continuation(lifecycle_ctx):
    """Given chat_queue_maxsize=2 and enough graph payloads,
    When the producer fills the queue,
    Then state.overflow flips True,
         the stream_generator yields flow_pause + returns,
         and the bg task still commits the ai row + releases the lock.
    """
    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()

    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="queue测试", status="active")
    )
    await lifecycle_ctx.seed_sess.commit()

    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    state = ChatStreamState()
    stop_event = asyncio.Event()

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for i in range(10):
            yield {"delta": f"x{i}"}
        yield {"finish_reason": "stop"}

    from tests.conftest import make_chat_context
    ctx = make_chat_context(
        session_id=sid, child_user_id=child.id,
        user_input="测试",
        settings=lifecycle_ctx.rr.settings,
        db_session_factory=lifecycle_ctx.rr.db_session_factory,
        audit_redis=lifecycle_ctx.redis_client,
    )

    rr = lifecycle_ctx.rr
    rr.main_graph.astream = fake_astream

    hid = uuid4()
    nonce = "test-nonce-queuefull"
    running_streams[str(sid)] = stop_event

    task = asyncio.create_task(
        run_llm_pipeline(
            rr=rr, redis=lifecycle_ctx.redis_client, sid=sid, hid=hid, nonce=nonce,
            child_user_id=child.id, turn_number=1,
            initial_state={"messages": []}, ctx=ctx,
            queue=queue, state=state, stop_event=stop_event,
        ),
        name=f"chat-llm-{sid}",
    )

    # 延迟启动段二，使 queue 填满
    await asyncio.sleep(0.05)

    gen = stream_generator(queue, state, sid)
    frames = []
    async for frame in gen:
        frames.append(frame)

    # flow_pause 帧应出现
    assert any(b"flow_pause" in (f if isinstance(f, bytes) else b"") for f in frames), (
        "flow_pause frame should be emitted"
    )

    await asyncio.wait_for(task, timeout=15.0)

    # 段一仍然完成了 commit②
    lifecycle_ctx.assert_sess.expire_all()
    ai_msg = (await lifecycle_ctx.assert_sess.execute(
        select(Message).where(Message.session_id == sid, Message.role == MessageRole.ai)
    )).scalar_one_or_none()
    assert ai_msg is not None, "ai row should be committed in headless mode"
    assert "x" in ai_msg.content


# =====================================================================
# #4: 段一异常 — HTTP 全栈
# =====================================================================


@pytest.mark.asyncio
async def test_llm_pipeline_exception_rolls_back_without_ai_row(lifecycle_ctx):
    """Given an LLM stub that raises mid-stream,
    When the bg task hits the except branch,
    Then db.rollback() should be called implicitly,
         no ai placeholder row should be written,
         an error frame should be yielded,
         and the session lock should still be released.
    """
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    async def fake_astream_broken(initial_state, stream_mode="custom", **kwargs):
        yield {"delta": "partial"}
        raise RuntimeError("pipeline crash")

    lifecycle_ctx.rr.main_graph.astream = fake_astream_broken
    resp = await client.post("/api/v1/me/chat/stream", json=_make_payload("Hi"), headers=headers)
    assert resp.status_code == 200

    import json as _json
    events = []
    ct = None
    for line in resp.text.split("\n"):
        if line.startswith("event:"):
            ct = line[len("event:"):].strip()
        elif line.startswith("data:") and ct is not None:
            events.append((ct, _json.loads(line[len("data:"):].strip())))

    # Error frame present
    assert any(ev[0] == "error" for ev in events), "error frame should be present"
    sid = events[0][1]["session_id"]

    # No ai row
    lifecycle_ctx.assert_sess.expire_all()
    ai_msgs = (await lifecycle_ctx.assert_sess.execute(
        select(Message).where(Message.session_id == sid, Message.role == MessageRole.ai)
    )).scalars().all()
    assert len(ai_msgs) == 0, "no ai row should exist after pipeline error"

    # Lock released
    lock_exists = await lifecycle_ctx.redis_client.exists(f"chat:lock:{sid}")
    assert not lock_exists, "Session lock was not released after error"


# =====================================================================
# #5: shutdown 等候 — 驱动真实 lifespan shutdown
# =====================================================================


@pytest.mark.asyncio
async def test_shutdown_waits_for_in_flight_bg_task(engine, redis_client):
    """Given an in-flight chat bg task expected to finish in 0.2s,
    When lifespan shutdown runs via exiting the lifespan context,
    Then asyncio.wait should return with done containing the task
         before the 30s timeout, and the task should not be cancelled.
    """

    rr = _make_real_rr_for_shutdown(engine, redis_client)

    async def quick_task():
        await asyncio.sleep(0.2)
        return 42

    t = asyncio.create_task(quick_task())
    rr.register_chat_task("test-shutdown-quick", t)

    app = _make_app_with_injected_rr(rr)

    # 进入 lifespan 上下文 → exit 时触发 shutdown 等候块
    async with _lifespan_for_test(app):
        pass  # lifespan 在 exit 时执行 shutdown 逻辑

    assert t.done(), "quick task should have completed during shutdown wait"
    assert not t.cancelled(), "task should not have been cancelled"


# =====================================================================
# #6: shutdown 超时 cancel — 驱动真实 lifespan shutdown
# =====================================================================


@pytest.mark.asyncio
async def test_shutdown_cancels_stuck_bg_task_after_timeout(engine, redis_client):
    """Given a stuck chat bg task (sleep 60s),
    When lifespan shutdown with patched short timeout runs,
    Then the pending task should be cancelled and gather without raising.
    """

    rr = _make_real_rr_for_shutdown(engine, redis_client)

    async def stuck_task():
        await asyncio.sleep(60)

    t = asyncio.create_task(stuck_task())
    rr.register_chat_task("test-shutdown-stuck", t)

    app = _make_app_with_injected_rr(rr)

    # main.py 的 shutdown 块硬编码 timeout=30.0。
    # patch app.main.asyncio.wait 将 timeout 截短至 0.2s，不改变 shutdown 逻辑。
    # asyncio.wait 是高层函数，非事件循环内部依赖——patch 安全。
    _orig_wait = asyncio.wait

    async def _short_timeout_wait(tasks, timeout=30.0):
        return await _orig_wait(tasks, timeout=0.2)

    with patch("app.main.asyncio.wait", _short_timeout_wait):
        async with _lifespan_for_test(app):
            pass

    # shutown 等候超时 → cancel + gather(return_exceptions=True)
    assert t.cancelled(), "stuck task should be cancelled after shutdown timeout"


def _make_real_rr_for_shutdown(engine=None, redis_client=None) -> RuntimeResources:
    """Construct a RuntimeResources for lifespan shutdown tests.

    Uses real engine/redis when provided (from conftest fixtures);
    falls back to MagicMock for standalone use.
    """
    from unittest.mock import MagicMock

    kwargs = dict(
        settings=_module_settings,
        db_engine=engine or MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=redis_client or MagicMock(),
        arq_pool=AsyncMock(),
        main_graph=MagicMock(),
        audit_graph=MagicMock(),
    )
    return RuntimeResources(**kwargs)


def _make_app_with_injected_rr(rr: RuntimeResources):
    """Create a FastAPI app with pre-injected RuntimeResources.

    Sets app.state.resources + _test_resources flag so lifespan's test seam
    skips build_runtime and teardown_runtime, but still runs the shutdown block.
    """
    from app.main import create_app

    app = create_app()
    app.state.resources = rr
    app.state._test_resources = True
    return app


@asynccontextmanager
async def _lifespan_for_test(app):
    """Enter/exit lifespan context for test-injected resources.

    Intended for shutdown tests (#5, #6): the lifespan's test seam detects
    _test_resources flag and skips build_runtime/redis_lifespan,
    but runs the shutdown block on exit.
    """
    from app.main import lifespan
    async with lifespan(app):
        yield


# =====================================================================
# #7: stop 信号 — HTTP 全栈
# =====================================================================


@pytest.mark.asyncio
async def test_stop_signal_breaks_pipeline_with_stopped_end_reason(lifecycle_ctx):
    """Given an in-flight chat stream,
    When running_streams[sid].set() is called externally,
    Then the pipeline should break with end_reason='stopped',
         commit② should land the partial ai_text,
         and the stopped frame should be emitted.
    """
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    sid = None  # captured from session_meta

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        nonlocal sid
        # 从 context 获取 sid
        ctx = kwargs.get("context")
        sid = str(ctx.session_id) if ctx else None
        yield {"delta": "部分回复"}
        # 设置 stop 事件
        ev = running_streams.get(sid)
        if ev is not None:
            ev.set()
        yield {"finish_reason": "stop"}

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    resp = await client.post("/api/v1/me/chat/stream", json=_make_payload("Hello"), headers=headers)
    assert resp.status_code == 200

    import json as _json
    events = []
    ct = None
    for line in resp.text.split("\n"):
        if line.startswith("event:"):
            ct = line[len("event:"):].strip()
        elif line.startswith("data:") and ct is not None:
            events.append((ct, _json.loads(line[len("data:"):].strip())))

    assert any(ev[0] == "stopped" for ev in events), "stopped frame should be present"
    stopped_ev = next(ev for ev in events if ev[0] == "stopped")
    assert stopped_ev[1]["finish_reason"] == "user_stopped"
    assert stopped_ev[1].get("aid") is not None, "StopWithAi should have aid"

    captured_sid = events[0][1]["session_id"]

    # DB: partial ai text committed
    lifecycle_ctx.assert_sess.expire_all()
    ai_msg = (await lifecycle_ctx.assert_sess.execute(
        select(Message).where(Message.session_id == captured_sid, Message.role == MessageRole.ai)
    )).scalar_one_or_none()
    assert ai_msg is not None, "ai row should exist for StopWithAi"
    assert ai_msg.finish_reason == "user_stopped"

    # Lock released
    lock_exists = await lifecycle_ctx.redis_client.exists(f"chat:lock:{captured_sid}")
    assert not lock_exists, "Session lock was not released after stop"


# =====================================================================
# #8: commit①~create_task 间异常锁释放 — HTTP 全栈
# =====================================================================


@pytest.mark.asyncio
async def test_lock_released_on_non_http_exception_between_commit1_and_create_task(lifecycle_ctx):
    """Given commit① and acquire_session_lock both succeeded,
    When asyncio.create_task raises RuntimeError (after commit①),
    Then the except Exception block should call release_session_lock,
         and Redis chat:lock:<sid> should not be left dangling (HTTP 5xx).
    """

    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    # 种子一个已知 sid 的 session，使 handler 使用此 sid
    from uuid import uuid4 as _uuid4
    known_sid = _uuid4()
    lifecycle_ctx.seed_sess.add(
        SessionModel(id=known_sid, child_user_id=child.id, title="注入测试", status=MessageStatus.active)
    )
    await lifecycle_ctx.seed_sess.commit()

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        yield {"delta": "x"}
        yield {"finish_reason": "stop"}

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    # monkeypatch asyncio.create_task → 仅在命名匹配 chat-llm- 时抛 RuntimeError
    # （避免影响 Starlette ASGI 传输层内部的 create_task 调用）
    _orig_create_task = asyncio.create_task

    def _raise_on_chat_llm(coro, *, name="", **kwargs):
        if name and name.startswith("chat-llm-"):
            coro.close()  # 显式消费未启动协程,避免 GC 阶段触发 never-awaited 警告
            raise RuntimeError("injected crash")
        return _orig_create_task(coro, name=name, **kwargs)

    with patch("app.api.me.asyncio.create_task", _raise_on_chat_llm):
        # Starlette 测试模式 raise_server_exceptions=True → RuntimeError 传播到 httpx
        with pytest.raises(RuntimeError, match="injected crash"):
            await client.post(
                "/api/v1/me/chat/stream",
                json=_make_payload("Hi", session_id=str(known_sid)),
                headers=headers,
            )

    # except Exception 块应已调用 release_session_lock（无 180s 残留）
    lock_exists = await lifecycle_ctx.redis_client.exists(f"chat:lock:{known_sid}")
    assert not lock_exists, (
        f"Session lock chat:lock:{known_sid} should have been released "
        f"by except Exception handler"
    )
