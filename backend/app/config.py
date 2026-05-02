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
    deepseek_model: str = "deepseek-chat"
    bailian_api_key: SecretStr = SecretStr("")  # required
    bailian_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    bailian_model: str = "qwen-plus"
    llm_request_timeout_seconds: float = 60.0

    model_config = {"env_prefix": "LB_", "env_file": ".env"}


settings = Settings()
