"""m8: add ai_turn_counter to sessions

Revision ID: 412aed826359
Revises: 96481d959825
Create Date: 2026-05-19 10:52:30.619586

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "412aed826359"
down_revision: Union[str, None] = "96481d959825"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """ADD COLUMN ai_turn_counter + backfill 已有 ai 消息的会话。"""
    op.add_column(
        "sessions",
        sa.Column(
            "ai_turn_counter",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment="LLM AI 回复累积轮次；persist_ai_turn 同事务 +1",
        ),
    )
    # backfill: 已有 ai 消息的行补真实计数值
    op.execute(
        "UPDATE sessions SET ai_turn_counter = ("
        "  SELECT COUNT(*) FROM messages"
        "  WHERE messages.session_id = sessions.id"
        "    AND messages.role::text = 'ai'"
        ")"
    )


def downgrade() -> None:
    """DROP COLUMN ai_turn_counter。"""
    op.drop_column("sessions", "ai_turn_counter")
