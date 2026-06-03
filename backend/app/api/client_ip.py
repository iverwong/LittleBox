"""客户端 IP 解析 —— 全局唯一的 IP 提取点。

反代部署契约 (重要):
    本函数不做 X-Forwarded-For / X-Real-IP 解析。那条路会引入
    "信任客户端伪造头" 的旁路:

        经 nginx ($proxy_add_x_forwarded_for) 后, 头变成
            X-Forwarded-For: <attacker>, <real-client>
        最左段恰恰是攻击者可控的那一段。取最左段等于让客户端
        自报家门, 限流被绕过, 也可定向投毒。

    即便契约要求 "uvicorn --forwarded-allow-ips=<反代 CIDR>" 配齐,
    该参数只净化 scope["client"] (uvicorn 层职责), 不会触碰 app
    自己直接读的原始 XFF 头。所以本函数绝不在 app 层解析 XFF。

    反代部署应让 uvicorn 的 ProxyHeadersMiddleware 完成 IP 净化:
        uvicorn app.main:app --proxy-headers \\
            --forwarded-allow-ips=<反代 IP 或 CIDR>
    uvicorn 据此:
        1. 校验直接 peer IP 是否在 --forwarded-allow-ips 白名单
        2. 是 → 取 XFF 最右一个非可信跳, 写入 scope["client"].host
        3. 否 → 忽略 XFF, scope["client"] 保留真实 peer IP

    之后本函数返回的就是 uvicorn 净化后的客户端 IP, 业务代码无
    任何额外信任判断, 也不再有可被伪造的接缝。

历史: 早期实现曾在 app 层做 XFF 最左段解析, 已删除。
trust_proxy_headers / LB_TRUST_PROXY_HEADERS 已同步移除, 不再使用。

None 语义:
    返回 None 表示 ASGI scope 未传 client (常见于裸 socket 部署
    或 ASGI 异常)。调用方 (如限流) 应将 None 视为"不参与该维度限流",
    而非塞进 "unknown" 共享桶。
"""

from __future__ import annotations

from fastapi import Request


def get_client_ip(request: Request) -> str | None:
    """返回 uvicorn 净化后的客户端 IP, 无 client 信息时返回 None。

    不解析 XFF / X-Real-IP / 任何代理头 —— 见模块级契约注释。
    反代净化由 uvicorn ProxyHeadersMiddleware 负责 (启动时
    配 --forwarded-allow-ips)。
    """
    if request.client and request.client.host:
        return request.client.host
    return None
