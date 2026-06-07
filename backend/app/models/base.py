"""Transitional shim — 6.B 临时保留,6.4 整体删除。

真正的 Base / BaseMixin / naming_convention 定义已迁至 app.core.db。
此处仅供 alembic/env.py 与 app.models.__init__ 在 6.E 改 import 路径前
继续走老 import 不断链使用;新代码应直接 from app.core.db import Base / BaseMixin。
"""

from app.core.db import Base, BaseMixin, naming_convention  # transitional; removed in 6.4

__all__ = ["Base", "BaseMixin", "naming_convention"]
