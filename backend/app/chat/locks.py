"""已迁出 — 留空 placeholder 至 Phase 6 整体删。

原内容(Redis 锁原语 + 进程级 stop event 登记表)Phase 3.1 拆为:
- 锁契约 → `app.core.locks`
- 进程级 stop event 登记表 → `app.domain.chat.stream_signals`
"""
