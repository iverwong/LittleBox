"""m11_daily_reports_unique_upsert

Revision ID: b656c65f93d4
Revises: c0e2a1ba8009
Create Date: 2026-06-23 15:31:10.600129

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b656c65f93d4'
down_revision: Union[str, None] = 'c0e2a1ba8009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('idx_reports_child', table_name='daily_reports')
    op.create_index('idx_reports_child', 'daily_reports', ['child_user_id', 'report_date'], unique=True)


def downgrade() -> None:
    op.drop_index('idx_reports_child', table_name='daily_reports')
    op.create_index('idx_reports_child', 'daily_reports', ['child_user_id', 'report_date'], unique=False)
