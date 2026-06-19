"""health 路由：Docker healthcheck 与外部监控使用的存活探针。"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """返回服务存活状态。

    极简实现：不连 DB / Redis,只证明 ASGI 进程在响应。
    完整存活/就绪检查由编排层(Docker / k8s)做。

    Returns:
        固定为 ``{"status": "ok"}`` 的字典。
    """
    return {"status": "ok"}
