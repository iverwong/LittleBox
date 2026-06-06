"""reconcile messages.finish_reason comment

补 messages.finish_reason 列的 PG COMMENT（与 ORM ``comment=...`` 字段
对齐）。b14be2a 创建列时未显式 COMMENT，alembic check 据此报
modify_comment 漂移。

ORM 同步：app/models/chat.py::Message.finish_reason 一直带
``comment="LLM finish_reason: stop/length/content_filter/user_stopped 等"``，
本迁移把这条注释落到 PG 元数据，使 alembic check 通过。

Revision ID: 0e5f4a1b9c2d
Revises: 0b8bb9b326a0
Create Date: 2026-06-06 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0e5f4a1b9c2d"
down_revision: Union[str, None] = "0b8bb9b326a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """把 ORM comment 落到 PG 元数据。"""
    op.execute(
        "COMMENT ON COLUMN messages.finish_reason IS "
        "'LLM finish_reason: stop/length/content_filter/user_stopped 等'"
    )


def downgrade() -> None:
    """撤销 COMMENT（恢复为无注释状态）。"""
    op.execute("COMMENT ON COLUMN messages.finish_reason IS NULL")
