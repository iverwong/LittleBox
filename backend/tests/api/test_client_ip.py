"""get_client_ip() 单点函数单元测试 + 限流降级回归测试。

覆盖矩阵:
- trust_proxy=False: 只用 peer IP; 拒绝代理头; 无 client → None
- trust_proxy=True: XFF 优先 → XRI 兜底 → peer 回退; 无 client 但有 XFF → XFF
- 限流: client=None 时不创建 login_fail:ip:* key, phone 桶仍生效
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.api.client_ip import get_client_ip
from app.config import Settings

# ---- helpers ----


def _make_request(
    *,
    client_host: str | None = "__omitted__",
    xff: str | None = None,
    xri: str | None = None,
) -> Request:
    """构造带可控 peer / headers 的 Starlette Request。

    client_host="__omitted__" (默认) → 不在 scope 里放 client key (模拟 ASGI spec
    允许的"无 client"情形)。client_host=None → 显式 None。其它字符串 → 填入 scope。
    """
    headers: list[tuple[bytes, bytes]] = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if xri is not None:
        headers.append((b"x-real-ip", xri.encode()))
    scope: dict = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
    }
    if client_host != "__omitted__":
        scope["client"] = [client_host, 5000] if client_host is not None else None
    return Request(scope)


# ---- get_client_ip 单元测试 ----


class TestGetClientIPTrustProxyFalse:
    """trust_proxy_headers=False: 只用 peer IP, 忽略代理头。"""

    def test_returns_peer_ip(self) -> None:
        req = _make_request(client_host="1.2.3.4")
        s = Settings(trust_proxy_headers=False)
        assert get_client_ip(req, s) == "1.2.3.4"

    def test_ignores_xff(self) -> None:
        """trust_proxy=False 时, 即便客户端伪造 XFF, 也不采纳 (防伪造头绕过限流)。"""
        req = _make_request(client_host="1.2.3.4", xff="9.9.9.9")
        s = Settings(trust_proxy_headers=False)
        assert get_client_ip(req, s) == "1.2.3.4"

    def test_ignores_xri(self) -> None:
        req = _make_request(client_host="1.2.3.4", xri="9.9.9.9")
        s = Settings(trust_proxy_headers=False)
        assert get_client_ip(req, s) == "1.2.3.4"

    def test_no_client_returns_none(self) -> None:
        """无 client 信息 → None, 不进 'unknown' 共享桶。"""
        req = _make_request(client_host="__omitted__")
        s = Settings(trust_proxy_headers=False)
        assert get_client_ip(req, s) is None

    def test_no_client_with_xff_returns_none(self) -> None:
        """trust_proxy=False 时, 即便有 XFF, 无 client → 仍 None (不靠代理头)。"""
        req = _make_request(client_host="__omitted__", xff="9.9.9.9")
        s = Settings(trust_proxy_headers=False)
        assert get_client_ip(req, s) is None


class TestGetClientIPTrustProxyTrue:
    """trust_proxy_headers=True: 代理头优先, peer 兜底。"""

    def test_xff_first_segment(self) -> None:
        req = _make_request(client_host="127.0.0.1", xff="5.6.7.8, 10.0.0.1")
        s = Settings(trust_proxy_headers=True)
        assert get_client_ip(req, s) == "5.6.7.8"

    def test_xff_single_segment(self) -> None:
        req = _make_request(client_host="127.0.0.1", xff="5.6.7.8")
        s = Settings(trust_proxy_headers=True)
        assert get_client_ip(req, s) == "5.6.7.8"

    def test_falls_back_to_xri(self) -> None:
        req = _make_request(client_host="127.0.0.1", xri="5.6.7.8")
        s = Settings(trust_proxy_headers=True)
        assert get_client_ip(req, s) == "5.6.7.8"

    def test_xff_preferred_over_xri(self) -> None:
        """XFF 与 XRI 同时存在时, XFF 胜 (标准 RFC 7239 顺序)。"""
        req = _make_request(client_host="127.0.0.1", xff="5.6.7.8", xri="9.9.9.9")
        s = Settings(trust_proxy_headers=True)
        assert get_client_ip(req, s) == "5.6.7.8"

    def test_falls_back_to_peer(self) -> None:
        """trust_proxy=True 但无代理头, 回退 peer IP (兼容直连 + trust 开的过渡期)。"""
        req = _make_request(client_host="127.0.0.1")
        s = Settings(trust_proxy_headers=True)
        assert get_client_ip(req, s) == "127.0.0.1"

    def test_no_client_with_xff_still_resolves(self) -> None:
        """无 peer 但有 XFF, 仍能解析 (裸 socket 不可达, 但反代头可读)。"""
        req = _make_request(client_host="__omitted__", xff="5.6.7.8")
        s = Settings(trust_proxy_headers=True)
        assert get_client_ip(req, s) == "5.6.7.8"

    def test_empty_xff_falls_through(self) -> None:
        """XFF 仅为空白 / 逗号时, 跳过, 尝试 XRI。"""
        req = _make_request(client_host="127.0.0.1", xff="  ,  ,", xri="5.6.7.8")
        s = Settings(trust_proxy_headers=True)
        assert get_client_ip(req, s) == "5.6.7.8"

    def test_no_client_no_headers_returns_none(self) -> None:
        req = _make_request(client_host="__omitted__")
        s = Settings(trust_proxy_headers=True)
        assert get_client_ip(req, s) is None


class TestGetClientIPDuckTyped:
    """settings 只需 trust_proxy_headers 字段; 测试可传 SimpleNamespace 替身。"""

    def test_duck_typed_settings(self) -> None:
        req = _make_request(client_host="1.2.3.4")
        s = SimpleNamespace(trust_proxy_headers=False)
        assert get_client_ip(req, s) == "1.2.3.4"

    def test_duck_typed_settings_true(self) -> None:
        req = _make_request(client_host="127.0.0.1", xff="5.6.7.8")
        s = SimpleNamespace(trust_proxy_headers=True)
        assert get_client_ip(req, s) == "5.6.7.8"


# ---- 限流降级回归 (集成层) ----


@pytest_asyncio.fixture
async def no_client_api_client(app):
    """构造一个 transport.client=None 的 AsyncClient, 模拟 ASGI scope 无 client。"""
    transport = ASGITransport(app=app)
    transport.client = None  # 强制 scope["client"] = None
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


class TestLoginNoClientIPRateLimit:
    """request.client=None 时: 限流应只走 phone 桶, 不创建 login_fail:ip:* key。"""

    @pytest.mark.asyncio
    async def test_phone_limit_still_triggers(
        self,
        no_client_api_client,
        seeded_parent,
    ) -> None:
        """同 phone 5 次错 → 第 6 次 429 (phone 桶仍生效)。"""
        user, _pw = seeded_parent
        for i in range(5):
            r = await no_client_api_client.post(
                "/api/v1/auth/login",
                json={
                    "phone": user.phone,
                    "password": f"wrongpw{i}",
                    "device_id": f"dev_no_ip_{i}",
                },
            )
            assert r.status_code == 401, f"attempt {i + 1}: {r.status_code}"
        r6 = await no_client_api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": "wrongpw_final", "device_id": "dev_no_ip_6"},
        )
        assert r6.status_code == 429

    @pytest.mark.asyncio
    async def test_no_ip_key_created_on_failures(
        self,
        no_client_api_client,
        redis_client,
        seeded_parent,
    ) -> None:
        """修复回归: 修复前所有 client=None 的失败会建 'login_fail:ip:unknown' key;
        修复后 IP 桶根本不被创建, 不会污染。"""
        user, _pw = seeded_parent
        for i in range(5):
            r = await no_client_api_client.post(
                "/api/v1/auth/login",
                json={
                    "phone": user.phone,
                    "password": f"wrongpw{i}",
                    "device_id": f"dev_no_ip_{i}",
                },
            )
            assert r.status_code == 401
        ip_keys = await redis_client.keys("login_fail:ip:*")
        assert ip_keys == [], f"IP 桶被污染: {ip_keys}"
        # 关键断言: phone 桶存在
        phone_keys = await redis_client.keys(f"login_fail:phone:{user.phone}")
        assert len(phone_keys) == 1

    @pytest.mark.asyncio
    async def test_no_ip_limit_even_with_many_cross_phone_failures(
        self,
        no_client_api_client,
        redis_client,
    ) -> None:
        """无 client 时, 跨 phone 25 次失败也不触发 IP 桶 429 (因 IP 桶不参与)。"""
        for i in range(25):
            r = await no_client_api_client.post(
                "/api/v1/auth/login",
                json={
                    "phone": f"nonexist_{i:04d}",
                    "password": "wrongpwxx",
                    "device_id": f"dev_x_{i}",
                },
            )
            assert r.status_code == 401, f"attempt {i + 1}: {r.status_code} {r.json()}"
        # 25 个 phone 桶各 count=1, 全部 < 5 → 不会有 429
        # 关键: 没有 IP 桶
        ip_keys = await redis_client.keys("login_fail:ip:*")
        assert ip_keys == []

    @pytest.mark.asyncio
    async def test_success_clears_phone_only(
        self,
        no_client_api_client,
        redis_client,
        seeded_parent,
        db_session,
    ) -> None:
        """成功登录清 phone 桶; 无 IP 桶, 不报错。"""
        user, pw = seeded_parent
        for i in range(3):
            await no_client_api_client.post(
                "/api/v1/auth/login",
                json={
                    "phone": user.phone,
                    "password": f"wrongpw{i}",
                    "device_id": f"dev_no_ip_{i}",
                },
            )
        # 此时 phone 桶存在
        assert await redis_client.get(f"login_fail:phone:{user.phone}") is not None
        # 成功
        r = await no_client_api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": "dev_no_ip_success"},
        )
        assert r.status_code == 200
        # 提交挂载的 delete ops
        await db_session.commit()
        # phone 桶被清
        assert await redis_client.get(f"login_fail:phone:{user.phone}") is None
        # 仍无 IP 桶
        ip_keys = await redis_client.keys("login_fail:ip:*")
        assert ip_keys == []
