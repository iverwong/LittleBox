"""_get_client_ip() 单点函数单元测试 + 限流降级回归测试。

契约:
- _get_client_ip(request) 只读 request.client.host, 不解析任何代理头
- 反代净化职责上移 uvicorn (--proxy-headers --forwarded-allow-ips)
- ASGI scope 无 client → 返回 None, 调用方应跳过 IP 维度限流

覆盖矩阵:
- peer IP 正常返回
- 客户端伪造 XFF / XRI 头被忽略 (防 XFF 解析旁路回归)
- 无 client → None
- 限流: client=None 时不创建 login_fail:ip:* key, phone 桶仍生效

历史: 函数原本在 app.api.client_ip 模块, 现已下沉为 auth.py 模块级
私有函数 (_get_client_ip), 因为它只有 auth.login 一处调用点。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.api.auth import _get_client_ip

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

    xff / xri 参数仅用于回归测试, 验证 _get_client_ip 即便收到伪造代理头
    也不解析 —— 这是 33db149→本补丁的契约变更。
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


# ---- _get_client_ip 单元测试 ----


class TestGetClientIP:
    """_get_client_ip(request) 只读 peer IP, 不解析任何代理头。"""

    def test_returns_peer_ip(self) -> None:
        req = _make_request(client_host="1.2.3.4")
        assert _get_client_ip(req) == "1.2.3.4"

    def test_no_client_returns_none(self) -> None:
        """无 client 信息 → None, 不进 'unknown' 共享桶。"""
        req = _make_request(client_host="__omitted__")
        assert _get_client_ip(req) is None

    def test_explicit_none_client_returns_none(self) -> None:
        req = _make_request(client_host=None)
        assert _get_client_ip(req) is None

    def test_empty_host_returns_none(self) -> None:
        """request.client.host 为空字符串 → None (与 None 等价处理)。"""
        req = _make_request(client_host="")
        assert _get_client_ip(req) is None


class TestGetClientIPIgnoresProxyHeaders:
    """回归: 33db149→本补丁删除了 app 层 XFF 解析。

    关键不变量: 即便客户端在 header 里塞 XFF / XRI, _get_client_ip 也不
    解析, 直接返回 peer IP。否则会重现最左段旁路
    (经 nginx 后 XFF[0] = 攻击者伪造, XFF[last] = 真实客户端)。
    """

    def test_ignores_xff_even_when_present(self) -> None:
        """XFF 解析旁路回归: 即便客户端伪造 XFF, _get_client_ip 也只读 peer IP。"""
        req = _make_request(client_host="1.2.3.4", xff="9.9.9.9, 5.6.7.8")
        assert _get_client_ip(req) == "1.2.3.4"

    def test_ignores_xri_even_when_present(self) -> None:
        req = _make_request(client_host="1.2.3.4", xri="9.9.9.9")
        assert _get_client_ip(req) == "1.2.3.4"

    def test_ignores_xff_when_no_peer(self) -> None:
        """无 peer + 伪造 XFF 仍返回 None —— 绝不靠代理头"补"出 IP。"""
        req = _make_request(client_host="__omitted__", xff="9.9.9.9")
        assert _get_client_ip(req) is None

    def test_attacker_rotating_xff_does_not_bypass(self) -> None:
        """XFF 轮换攻击回归: 即便攻击者每请求换一个 XFF[0], _get_client_ip
        看到的 peer 不变, IP 限流仍能累计到阈值。"""
        # 模拟 25 次攻击, 每次 XFF[0] 都不同, peer IP 始终是攻击者自己的
        for i in range(25):
            req = _make_request(client_host="attacker.1.2.3.4", xff=f"victim.{i}.9.9.9, 5.6.7.8")
            assert _get_client_ip(req) == "attacker.1.2.3.4"


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
