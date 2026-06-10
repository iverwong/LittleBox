"""M8 audit / arq settings 默认值验证（Step 4 后只验非 LLM 字段）。"""
from app.core.config import Settings


def test_audit_settings_defaults():
    """4 个 audit / arq 非 LLM settings 可读取默认值（其余 LLM 字段已迁 llm_topology）。"""
    s = Settings()
    assert s.audit_wait_timeout_seconds == 30
    assert s.audit_redis_ttl_seconds == 86400
    assert s.arq_redis_db == 1
    assert s.max_audit_tool_iterations == 5


def test_arq_redis_db_isolation():
    """arq_redis_db=1 与业务 db=0 不同。"""
    s = Settings()
    assert s.arq_redis_db != 0
