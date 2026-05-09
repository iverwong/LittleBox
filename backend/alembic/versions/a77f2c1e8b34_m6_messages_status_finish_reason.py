"""m6 messages status finish_reason columns and partial indexes

Revision ID: a77f2c1e8b34
Revises: 3522d5e7ba69
Create Date: 2026-05-01 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a77f2c1e8b34'
down_revision: Union[str, None] = '3522d5e7ba69'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create PG ENUM type messagestatus (aligned with SessionStatus pattern: sa.Enum + create)
    message_status = sa.Enum('active', 'discarded', name='messagestatus')
    message_status.create(op.get_bind(), checkfirst=True)

    # Add columns
    op.add_column(
        'messages',
        sa.Column('status', message_status, nullable=False, server_default='active'),
    )
    op.add_column(
        'messages',
        sa.Column('finish_reason', sa.String(length=50), nullable=True),
    )

    # Drop M3-era indexes (replaced by partial indexes below)
    op.drop_index('idx_messages_session', table_name='messages')
    op.drop_index('idx_sessions_child', table_name='sessions')

    # M6 partial indexes: keyset pagination + WHERE status='active' read paths
    op.create_index(
        'idx_messages_session_active_created',
        'messages',
        ['session_id', sa.text('created_at DESC'), sa.text('id DESC')],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        'idx_sessions_child_active_lastactive',
        'sessions',
        ['child_user_id', sa.text('last_active_at DESC'), sa.text('id DESC')],
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    # Reverse partial indexes (restore M3-era)
    op.drop_index('idx_sessions_child_active_lastactive', table_name='sessions')
    op.drop_index('idx_messages_session_active_created', table_name='messages')
    op.create_index('idx_sessions_child', 'sessions', ['child_user_id', 'status'])
    op.create_index('idx_messages_session', 'messages', ['session_id', 'created_at'])

    # Reverse columns
    op.drop_column('messages', 'finish_reason')
    op.drop_column('messages', 'status')

    # Drop PG ENUM type
    sa.Enum(name='messagestatus').drop(op.get_bind(), checkfirst=True)
