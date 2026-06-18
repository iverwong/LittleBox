from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置，从环境变量读取。"""

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/littlebox"
    redis_url: str = "redis://localhost:6379/0"
    app_name: str = "LittleBox"
    debug: bool = False
    cors_origins: list[str] = ["*"]
    # LLM provider 密钥（env 注入 LB_DEEPSEEK_API_KEY / LB_BAILIAN_API_KEY）。
    # 其余 LLM 拓扑（base_url / model / role 绑定 / thinking / reasoning_effort /
    # timeout / fallback）全部入 app/core/llm_topology.py，settings 不再承载。
    deepseek_api_key: SecretStr = SecretStr("")  # required
    bailian_api_key: SecretStr = SecretStr("")  # required
    # M9 三级干预上下文参数（limit 直接是消息条数，非"对"数）
    crisis_context_recent_messages: int = 10  # crisis anchor_window 消息数（含触发轮,等价 5 对）
    # M5 hotfix: family child count limit
    max_children_per_family: int = 3
    # M8 主图轮询审查结果超时秒数
    audit_wait_timeout_seconds: int = 30
    # M8 审查信号管道 Redis TTL（秒），默认 24h
    audit_redis_ttl_seconds: int = 86400
    # M8 ARQ 队列 Redis db 编号，与业务 db=0 隔离
    arq_redis_db: int = 1
    # M8 session_notes tool agentic loop 硬上限
    max_audit_tool_iterations: int = 5
    # M9-patch1: 段一段二队列上限（默认 128），约 2.5s 弱网缓冲
    chat_queue_maxsize: int = 128
    # M9-patch1: 段一每帧降速钩子（默认 0.0 不触发，>0 时段一每帧 sleep）
    chat_stream_interval_s: float = 0.0

    model_config = {"env_prefix": "LB_", "env_file": ".env"}


settings = Settings()
