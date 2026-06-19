from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置（pydantic-settings，从环境变量读取）。

    环境变量统一以 `LB_` 为前缀（如 `LB_DATABASE_URL` / `LB_REDIS_URL` /
    `LB_DEEPSEEK_API_KEY` 等），同时从 `.env` 文件加载。LLM 拓扑相关字段
    （base_url / 模型名 / role 绑定 / thinking / reasoning_effort / timeout /
    fallback）由 `app/core/llm_topology.py` 承担，`Settings` 不再承载。
    """

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/littlebox"
    redis_url: str = "redis://localhost:6379/0"
    app_name: str = "LittleBox"
    debug: bool = False
    cors_origins: list[str] = ["*"]
    # LLM provider 密钥。环境变量注入：`LB_DEEPSEEK_API_KEY` / `LB_BAILIAN_API_KEY`。
    deepseek_api_key: SecretStr = SecretStr("")  # 必填（生产环境注入）
    bailian_api_key: SecretStr = SecretStr("")  # 必填（生产环境注入）
    # crisis 三级干预上下文：anchor_window 拉取的消息条数（含触发轮本身）。
    crisis_context_recent_messages: int = 10
    # 单个家庭允许创建的最大孩子账号数。
    max_children_per_family: int = 3
    # 主图轮询审查结果的最大等待秒数。
    audit_wait_timeout_seconds: int = 30
    # 审查信号管道在 Redis 上的 TTL（秒），默认 24 小时。
    audit_redis_ttl_seconds: int = 86400
    # ARQ 队列使用的 Redis db 编号，与业务 db=0 隔离。
    arq_redis_db: int = 1
    # audit session_notes 工具的 agentic loop 硬上限。
    max_audit_tool_iterations: int = 5
    # chat 流式生成与 SSE 转发之间的有界队列容量（约 2.5 秒弱网缓冲）。
    chat_queue_maxsize: int = 128
    # chat 流式生成每帧降速钩子（秒）。0.0 表示不触发；>0 时每帧额外 sleep。
    chat_stream_interval_s: float = 0.0

    model_config = {"env_prefix": "LB_", "env_file": ".env"}


settings = Settings()
