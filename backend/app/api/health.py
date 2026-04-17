from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """健康检查端点，Docker healthcheck 和外部监控使用。"""
    return {"status": "ok"}
