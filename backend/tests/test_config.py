"""M8 audit settings 默认值验证。"""
from app.config import Settings


def test_audit_settings_defaults():
    """8 个 audit / arq settings 可读取默认值。"""
    s = Settings()
    assert s.audit_provider == "deepseek"
    assert s.audit_model == "deepseek-v4-flash"
    assert s.audit_reasoning_effort == "max"
    assert s.audit_thinking_enabled is True
    assert s.audit_wait_timeout_seconds == 30
    assert s.audit_redis_ttl_seconds == 86400
    assert s.arq_redis_db == 1
    assert s.max_audit_tool_iterations == 5


def test_arq_redis_db_isolation():
    """arq_redis_db=1 与业务 db=0 不同。"""
    s = Settings()
    assert s.arq_redis_db != 0
