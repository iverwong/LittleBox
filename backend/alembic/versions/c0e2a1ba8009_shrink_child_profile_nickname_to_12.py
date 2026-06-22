"""shrink_child_profile_nickname_to_12

Revision ID: c0e2a1ba8009
Revises: 2c30b6405a18
Create Date: 2026-06-22 10:08:09.860486

只收紧 child_profiles.nickname 列长度(32 → 12)。

上线前必须先跑探测,确认无超长行,否则 PG 会因截断报错:

    SELECT count(*) FROM child_profiles WHERE char_length(nickname) > 12;
    -- 期望: 0

注:alembic autogenerate 把所有列 comment 的全 / 半角标点差异也当成了
diff,本迁移人工精简后只保留 nickname 长度变更(其他 comment diff 在
本任务范围外,不在此迁移处理)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c0e2a1ba8009"
down_revision: Union[str, None] = "2c30b6405a18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """nickname String(32) → String(12)。"""
    op.alter_column(
        "child_profiles",
        "nickname",
        existing_type=sa.String(length=32),
        type_=sa.String(length=12),
        existing_nullable=False,
    )


def downgrade() -> None:
    """nickname String(12) → String(32)。"""
    op.alter_column(
        "child_profiles",
        "nickname",
        existing_type=sa.String(length=12),
        type_=sa.String(length=32),
        existing_nullable=False,
    )