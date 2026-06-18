"""drop redline fields from audit_records

Revision ID: 2c30b6405a18
Revises: 0e5f4a1b9c2d
Create Date: 2026-06-18 04:47:16.662459

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '2c30b6405a18'
down_revision: Union[str, None] = '0e5f4a1b9c2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop redline columns from audit_records
    op.drop_column('audit_records', 'redline_triggered')
    op.drop_column('audit_records', 'redline_detail')


def downgrade() -> None:
    # Re-add redline columns to audit_records
    op.add_column('audit_records',
        sa.Column('redline_triggered', sa.Boolean(), server_default=sa.text('false'), nullable=False)
    )
    op.add_column('audit_records',
        sa.Column('redline_detail', sa.Text(), nullable=True)
    )
