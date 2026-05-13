"""m6_patch3_scheme_r_context_size_needs_compression

方案 R 落地：
- RENAME sessions.context_token_count → context_size_tokens INT NULL
- ADD  sessions.needs_compression BOOLEAN NOT NULL DEFAULT FALSE
- ADD VALUE 'summary' TO TYPE messagerole
- ADD VALUE 'compressed' TO TYPE messagestatus
- upgrade 保留 partial indexes（生产性能最优）；
  downgrade 暂 drop → 列变更 → 重建 partial（规避 WHERE 子句 <-> 列类型耦合）

Revision ID: 96481d959825
Revises: 84781fbc465a
Create Date: 2026-05-13 02:41:48.427881

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '96481d959825'
down_revision: Union[str, None] = '84781fbc465a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """upgrade 保留 partial indexes（idx_messages_session_active_created 等持续优化 build_context 热点查询）。"""
    # （1）messages 枚举扩值（先 enum，后列变更）
    op.execute("ALTER TYPE messagerole ADD VALUE 'summary'")
    op.execute("ALTER TYPE messagestatus ADD VALUE 'compressed'")

    # （2）RENAME COLUMN 保留历史数据（旧 0 值不洗，新逻辑读 0 不触发压缩）
    op.alter_column("sessions", "context_token_count",
                    new_column_name="context_size_tokens",
                    nullable=True)

    # （3）新增标志列
    op.add_column("sessions",
                  sa.Column("needs_compression", sa.Boolean(),
                            server_default=sa.text("false"),
                            nullable=False))


def downgrade() -> None:
    # ---- 前置：drop partial indexes（WHERE 子句引用 messagestatus，列类型变更前必须清除）----
    op.drop_index("idx_messages_session_active_created", table_name="messages")
    op.drop_index("idx_sessions_child_active_lastactive", table_name="sessions")

    # ---- messages: 枚举回退 ----
    # PG 不支持 DROP VALUE，采用「转 text → drop → create → 转回」四步式
    # 前置清理：summary 行 delete（有损回退，压缩本来就是有损操作）；
    # compensated 行降级为 discarded（放弃本次压缩成果）
    op.execute("DELETE FROM messages WHERE role = 'summary'")
    op.execute("UPDATE messages SET status = 'discarded' WHERE status = 'compressed'")

    # MessageRole: 通过 text 中转
    op.execute("ALTER TABLE messages ALTER COLUMN role TYPE text USING role::text")
    op.execute("DROP TYPE IF EXISTS messagerole")
    op.execute("CREATE TYPE messagerole AS ENUM ('human', 'ai')")
    op.execute(
        "ALTER TABLE messages ALTER COLUMN role TYPE messagerole "
        "USING role::messagerole"
    )

    # MessageStatus: 先 DROP DEFAULT（DEFAULT 引用了旧 enum），再通过 text 中转
    op.execute("ALTER TABLE messages ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE messages ALTER COLUMN status TYPE text USING status::text")
    op.execute("DROP TYPE IF EXISTS messagestatus")
    op.execute("CREATE TYPE messagestatus AS ENUM ('active', 'discarded')")
    op.execute(
        "ALTER TABLE messages ALTER COLUMN status TYPE messagestatus "
        "USING status::messagestatus"
    )
    op.execute("ALTER TABLE messages ALTER COLUMN status SET DEFAULT 'active'")

    # ---- sessions: 列回退 ----
    op.drop_column("sessions", "needs_compression")

    # 反向 rename 前把 NULL 值恢复为 0（原列 NOT NULL DEFAULT 0）
    op.execute("UPDATE sessions SET context_size_tokens = 0 WHERE context_size_tokens IS NULL")
    op.alter_column("sessions", "context_size_tokens",
                    new_column_name="context_token_count",
                    nullable=False)

    # ---- 重建 partial indexes（与迁移 a77f2c1e8b34 的 upgrade 一致）----
    op.create_index(
        "idx_messages_session_active_created",
        "messages",
        ["session_id", sa.text("created_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "idx_sessions_child_active_lastactive",
        "sessions",
        ["child_user_id", sa.text("last_active_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("status = 'active'"),
    )
