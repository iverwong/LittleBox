"""m9: add turn_number, crisis_locked_message_id, target_message_id

Revision ID: 0b8bb9b326a0
Revises: 412aed826359
Create Date: 2026-05-28 09:59:39.680520

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0b8bb9b326a0"
down_revision: Union[str, None] = "412aed826359"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """ADD 3 列 + DROP crisis_locked + backfill turn_number。

    C1 修正：使用 SUM() OVER (PARTITION BY session_id ORDER BY created_at)
    累加 human indicator，确保 orphan human 存在时编号仍正确对齐。
    """
    op.add_column(
        "messages",
        sa.Column(
            "turn_number",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment="对话轮次编号。human/ai 同轮共享同号；summary/discarded 行保持 0。"
            "由 Step 3 commit①/commit② 与 backfill SQL 共同维护",
        ),
    )
    op.add_column(
        "rolling_summaries",
        sa.Column(
            "crisis_locked_message_id",
            postgresql.UUID(),
            nullable=True,
            comment="crisis 粘性接管锚点消息 ID。非空=粘性锁定中，"
            "指向触发 crisis 的首条 ai_msg id；"
            "空=未锁定。session 内不可逆，仅开启新 session 可重置。",
        ),
    )
    op.drop_column("rolling_summaries", "crisis_locked")
    op.add_column(
        "audit_records",
        sa.Column(
            "target_message_id",
            postgresql.UUID(),
            nullable=True,
            comment="被审查的 ai_msg id（本轮审查锚点）。"
            "M9 新增，由 enqueue_audit 从 me.py generator 传入",
        ),
    )
    # backfill: active human/ai 行按出现顺序编号，human 出现即"新一轮"
    # C1 修正：SUM() OVER 累加 human indicator，而非 PARTITION BY role
    # orphan human 存在时 ai 不跳号，保证 human/ai 同轮共享同号
    op.execute("""
        WITH numbered AS (
          SELECT id,
            SUM(CASE WHEN role='human' THEN 1 ELSE 0 END)
              OVER (PARTITION BY session_id ORDER BY created_at
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS turn
          FROM messages
          WHERE status='active' AND role IN ('human','ai')
        )
        UPDATE messages m SET turn_number = n.turn FROM numbered n WHERE m.id = n.id;
    """)


def downgrade() -> None:
    """DROP 3 列 + 恢复 crisis_locked boolean（数据丢失可接受，production 无 true 数据）。"""
    op.drop_column("audit_records", "target_message_id")
    op.drop_column("messages", "turn_number")
    op.add_column(
        "rolling_summaries",
        sa.Column(
            "crisis_locked",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="crisis 粘性接管标志。一旦命中 crisis 置 true，"
            "该 session 剩余轮次全部由危机 LLM 接管；"
            "session 内不可逆，仅开启新 session 可重置。"
            "redline 不粘性，每轮重评估。",
        ),
    )
    op.drop_column("rolling_summaries", "crisis_locked_message_id")
