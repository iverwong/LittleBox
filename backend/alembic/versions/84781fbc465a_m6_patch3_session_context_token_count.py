"""m6_patch3_session_context_token_count

Revision ID: 84781fbc465a
Revises: a77f2c1e8b34
Create Date: 2026-05-11 09:30:05.719018

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '84781fbc465a'
down_revision: Union[str, None] = 'a77f2c1e8b34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("context_token_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sessions", "context_token_count")
