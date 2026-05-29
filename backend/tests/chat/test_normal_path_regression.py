"""Normal path SSE 事件序列回归（T15 C3）。

通过 api_client 走真实 ASGI 栈 + mock _main_graph.astream（**kwargs 接收 context=），
断言 SSE 事件序列 session_meta → delta → end 与 M8 一致。

不打真实 LLM，DB 写入经 api_client transactional rollback 隔离（M5 隔离铁律）。
与 test_sse.py 互补（后者测 SSE 格式化工具函数，本文件测 ASGI 全栈 SSE 事件序列）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.asyncio

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _parse_sse_frames(raw: str) -> list[dict]:
    """解析 SSE 事件帧为 dict 列表。"""
    events = []
    current_type = None
    for line in raw.split("\n"):
        if line.startswith("event:"):
            current_type = line[len("event:"):].strip()
        elif line.startswith("data:") and current_type is not None:
            events.append({"type": current_type, "data": json.loads(line[len("data:"):].strip())})
    return events


@pytest.fixture
async def auth_headers_child(db_session, redis_client):
    """创建 child 用户 + token，返回 headers。"""
    from app.auth.redis_ops import commit_with_redis
    from app.auth.tokens import issue_token
    from app.models.accounts import ChildProfile, Family, FamilyMember, User
    from app.models.enums import Gender, UserRole

    fam = Family()
    db_session.add(fam)
    await db_session.flush()
    user = User(family_id=fam.id, role=UserRole.child, phone="0009", is_active=True)
    db_session.add(user)
    await db_session.flush()
    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    profile = ChildProfile(
        child_user_id=user.id, created_by=user.id,
        birth_date=datetime(2016, 6, 1, tzinfo=SHANGHAI).date(),
        gender=Gender.male, nickname="sse-test",
    )
    db_session.add(profile)
    await db_session.flush()
    device_id = "sse-device"
    token = await issue_token(
        db_session, user_id=user.id, role=UserRole.child,
        family_id=user.family_id, device_id=device_id, ttl_days=None,
    )
    await commit_with_redis(db_session, redis_client)
    return {"Authorization": f"Bearer {token}", "X-Device-Id": device_id}, user


@pytest.fixture(autouse=True)
def _mock_enqueue_audit():
    """mock enqueue_audit 避免 audit Redis 依赖。"""
    with patch("app.api.me.enqueue_audit", AsyncMock()):
        yield


@pytest.fixture(autouse=True)
def _patch_locks(monkeypatch: pytest.MonkeyPatch):
    """绕过 throttle + session lock。"""
    from app.chat.locks import acquire_session_lock, acquire_throttle_lock
    monkeypatch.setattr("app.api.me.acquire_throttle_lock", AsyncMock(return_value=True))
    monkeypatch.setattr("app.api.me.acquire_session_lock", AsyncMock(return_value="mock-nonce"))
    monkeypatch.setattr("app.api.me.release_session_lock", AsyncMock(return_value=None))


async def test_normal_path_sse_sequence(api_client, auth_headers_child, app):
    """session_meta → delta → end SSE 事件序列回归。

    Mock graph.astream 含 **kwargs 兼容 context= 参数（C3），
    yield delta/finish_reason/usage_metadata 模拟 LLM 流式输出。
    """
    headers, _ = auth_headers_child

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        """Mock LangGraph astream（**kwargs 接收 context= 参数）。"""
        yield {"delta": "你好"}
        yield {"delta": "今天"}
        yield {"finish_reason": "stop"}
        yield {"usage_metadata": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}

    app.state.resources.main_graph.astream = fake_astream
    resp = await api_client.post(
        "/api/v1/me/chat/stream",
        json={"content": "你好"},
        headers=headers,
    )
    assert resp.status_code == 200
    await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    types = [f["type"] for f in frames]

    assert "session_meta" in types, "缺少 session_meta 帧"
    assert types[0] == "session_meta", "首帧应为 session_meta"

    delta_indices = [i for i, t in enumerate(types) if t == "delta"]
    assert len(delta_indices) >= 1, "至少应有 1 个 delta 帧"

    assert "end" in types, "缺少 end 帧"
    assert types[-1] == "end", "末帧应为 end"
