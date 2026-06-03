"""客户端 IP 解析 —— 全局唯一的 IP 提取点。

散落在各路由里的 `request.client.host` 读取行为在此收敛;
未来部署到反代后 (nginx / ALB) 时, 只需在 Settings 开 trust_proxy_headers
+ uvicorn 启动加 --forwarded-allow-ips, 即可识别真实客户端 IP, 业务代码无改动。

None 语义:
    返回 None 表示"无法解析出可信的客户端 IP" —— 通常是
    (a) 裸 socket 部署且 ASGI scope 未传 client
    (b) request.client.host 为空
    调用方 (如限流) 应将 None 视为"不参与该维度限流", 而非塞进
    "unknown" 共享桶 —— 共享桶会被不同物理客户端合并, 触发误伤式 DoS。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastapi import Request

if TYPE_CHECKING:
    pass


class _SettingsLike(Protocol):
    """仅依赖 trust_proxy_headers 字段, 便于单测传入轻量替身。"""

    trust_proxy_headers: bool


def get_client_ip(request: Request, settings: _SettingsLike) -> str | None:
    """解析 request 的客户端 IP, 无法解析时返回 None。

    Args:
        request: FastAPI/Starlette Request 对象。
        settings: 任意含 `trust_proxy_headers: bool` 字段的对象
            (生产用 app.config.Settings, 单测可传 SimpleNamespace 等)。

    Returns:
        解析到的 IP 字符串; 解析不到返回 None (NOT "unknown")。

    解析优先级 (trust_proxy_headers=True 时):
        1. X-Forwarded-For 首段 (逗号分隔, 去空白)
        2. X-Real-IP
        3. 回退到 request.client.host

    trust_proxy_headers=False 时:
        1. 直接返回 request.client.host
        2. request.client 为 None 或 host 为空 → 返回 None
    """
    if settings.trust_proxy_headers:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
        xri = request.headers.get("x-real-ip")
        if xri:
            stripped = xri.strip()
            if stripped:
                return stripped

    if request.client and request.client.host:
        return request.client.host
    return None
