"""测试基建的表名单一来源。

所有 conftest fixture 与 expert 域测试的 TRUNCATE / 行数 guard 共享这份列表,
避免「子集 TRUNCATE 漏表」导致跨测试污染(历史教训:M11 expert 上线后
`tests/expert/test_repository.py` 的 _TABLES 漏掉 families + family_members,
11 个测试每个留 1 个 family 残留,把后续 test_conftest_smoke 的基线推高 11)。

修改时请同步确认所有引用方仍兼容。
"""

from __future__ import annotations

_GUARD_TABLES: list[str] = [
    "families",
    "users",
    "child_profiles",
    "auth_tokens",
    "device_tokens",
    "family_members",
    "sessions",
    "messages",
    "audit_records",
    "rolling_summaries",
    "turn_summaries",
    "daily_reports",
    "notifications",
    "data_deletion_requests",
]
"""全量 14 张业务表 — 任何「清理测试库 / 兜底 prod 库行数」的 fixture 都应引用此列表。

历史教训(2026-06-24 调研):expert 域测试 `_TABLES` 漏掉 families + family_members,
concurrent_db_sessions teardown TRUNCATE 不清这两张,11 个测试每个留 1 行污染。
"""
