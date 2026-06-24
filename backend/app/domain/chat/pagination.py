"""Cursor(keyset pagination)编码与解码。

list_sessions / get_messages 走 keyset 游标分页,游标为 base64
编码的 "<sort_key_iso>|<row_id>" 二元组。本模块从 api/me.py 抽离,
公开 API:

- `InvalidCursor` —— 游标解码失败时抛出的 HTTPException (400)
- `encode_cursor(sort_key, row_id)` —— base64 URL-safe 编码
- `decode_cursor(cursor)` —— 解码,失败时抛 `InvalidCursor`

排序键统一 strip tzinfo(naive)：时区信息在 cursor 编码中冗余,strip 后编码
更紧凑、解码更简单;DB 列实际为 TIMESTAMPTZ,比较由 PostgreSQL 把 naive
datetime 当作 session timezone 处理,编码/解码两端保持同一条规则即可。
"""

from __future__ import annotations

import base64
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status


class InvalidCursor(HTTPException):
    """游标解码失败(base64 损坏 / ISO 格式非法 / UUID 非法)统一抛此异常。"""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="InvalidCursor",
        )


def encode_cursor(sort_key: datetime, row_id: str) -> str:
    """编码 (sort_key, row_id) 为 URL-safe base64 字符串。

    Args:
        sort_key: 排序键时间戳(若带 tzinfo 会被剥离为 naive)。
        row_id: 关联行 id(字符串形式)。

    Returns:
        URL-safe base64 编码的游标字符串。
    """
    if sort_key.tzinfo is not None:
        sort_key = sort_key.replace(tzinfo=None)
    return base64.urlsafe_b64encode(f"{sort_key.isoformat()}|{row_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解码游标为 (sort_key, row_id)。

    Args:
        cursor: URL-safe base64 编码的游标字符串。

    Returns:
        (sort_key_dt, row_id) 元组,sort_key 为 naive datetime,row_id 为字符串。

    Raises:
        InvalidCursor: base64 损坏、ISO 格式非法或 UUID 非法。
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except Exception:
        raise InvalidCursor()

    parts = raw.rsplit("|", 1)
    if len(parts) != 2:
        raise InvalidCursor()
    sort_key_str, row_id = parts

    sort_key_dt: datetime | None = None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(sort_key_str, fmt)
            sort_key_dt = dt.replace(tzinfo=None)
            break
        except ValueError:
            continue
    _naive_fmts = (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    )
    for fmt in _naive_fmts:
        try:
            sort_key_dt = datetime.strptime(sort_key_str, fmt)
            break
        except ValueError:
            continue
    if sort_key_dt is None:
        raise InvalidCursor()

    try:
        UUID(row_id)
    except Exception:
        raise InvalidCursor()

    return sort_key_dt, row_id
