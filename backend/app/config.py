from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置，从环境变量读取。"""

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/littlebox"
    redis_url: str = "redis://localhost:6379/0"
    app_name: str = "LittleBox"
    debug: bool = False
    cors_origins: list[str] = ["*"]

    model_config = {"env_prefix": "LB_", "env_file": ".env"}


settings = Settings()
