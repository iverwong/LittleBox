from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置，从环境变量读取。"""

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/littlebox"
    redis_url: str = "redis://localhost:6379/0"
    app_name: str = "LittleBox"
    debug: bool = False
    cors_origins: list[str] = ["*"]
    # LLM providers (M6 Step 2.5)
    deepseek_api_key: SecretStr = SecretStr("")  # required
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"
    bailian_api_key: SecretStr = SecretStr("")  # required
    bailian_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    bailian_model: str = "deepseek-v4-flash"
    # M6 provider abstraction
    main_provider: str = "deepseek"
    fallback_provider: str | None = "deepseek"
    enable_fallback: bool = True
    # M9 main LLM 推理深度和思考模式（统一高，提升主对话推理深度）
    main_thinking_enabled: bool = True
    main_reasoning_effort: str = "max"
    # M9 三级干预上下文参数
    crisis_context_recent_turns: int = 5  # crisis anchor_window 对数（含触发轮）
    redline_context_recent_turns: int = 10  # 红线最近活跃对数
    redline_turn_summaries_window: int = 50  # 红线摘要窗口条数
    llm_request_timeout_seconds: float = 60.0
    # M5 hotfix: family child count limit
    max_children_per_family: int = 3
    # M8 审查 LLM 供应商标识，与 _PROVIDER_REGISTRY 注册名对齐
    audit_provider: str = "deepseek"
    # M8 审查 LLM 模型名
    audit_model: str = "deepseek-v4-flash"
    # M8 推理深度：thinking 模式下 low/medium 被服务端映射为 high，显式 max 才拿最深推理
    audit_reasoning_effort: str = "max"
    # M8 启用思考模式（extra_body={"thinking":{"type":"enabled"}}），与 reasoning_effort 联动
    audit_thinking_enabled: bool = True
    # M8 主图轮询审查结果超时秒数
    audit_wait_timeout_seconds: int = 30
    # M8 审查信号管道 Redis TTL（秒），默认 24h
    audit_redis_ttl_seconds: int = 86400
    # M8 ARQ 队列 Redis db 编号，与业务 db=0 隔离
    arq_redis_db: int = 1
    # M8 session_notes tool agentic loop 硬上限
    max_audit_tool_iterations: int = 5
    # M8 压缩 LLM 供应商标识，与 _PROVIDER_REGISTRY 注册名对齐
    compression_provider: str = "deepseek"
    # M8 压缩 LLM 模型名
    compression_model: str = "deepseek-v4-flash"
    # M8 压缩启用思考模式（默认关闭，压缩任务不需要 reasoning）
    compression_thinking_enabled: bool = False

    model_config = {"env_prefix": "LB_", "env_file": ".env"}


settings = Settings()
