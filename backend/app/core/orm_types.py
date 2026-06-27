"""Pydantic <-> JSONB 通用序列化，跨域基础设施叶子。

`PydanticJSONB` 是所有 JSON/JSONB 列承载 Pydantic 配置类时的统一入口：
- 写库：`dump_python(value, mode="json", warnings="error")` 把 Pydantic
  实例序列化为 JSON 兼容 dict（datetime/Decimal/UUID/Enum 自动转字符串）；
  遇到 Pydantic 不认识的类型,`warnings="error"` 立即 raise 而非静默丢失。
- 读库：`validate_python(value)` 还原为 Pydantic 实例；`ValidationError`
  透传,不在 ORM 层吞掉,让上层看到结构损坏。

配套约束：承载此 TypeDecorator 的配置类应使用
`model_config = ConfigDict(frozen=True)`,避免原地修改绕过 SQLAlchemy
脏检测导致 UPDATE 写不出去。
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator


class PydanticJSONB(TypeDecorator):
    """Pydantic 模型与 PostgreSQL JSONB 之间的双向序列化。

    Attributes:
        impl: 底层 SQLAlchemy 类型,固定为 JSONB。
        cache_ok: 启用 SA 装饰器缓存(对相同 impl 类型复用同一实例)。
    """

    impl = JSONB
    cache_ok = True

    def __init__(self, python_type: Any) -> None:
        """初始化 TypeAdapter 与底层 JSONB impl。

        透传 `python_type` 给 `super().__init__()` 触发 `TypeDecorator.__init__`
        把 `self.impl(*args)` 存为 `self.impl_instance`,供 SA 反射
        `load_dialect_impl` 与 `dialect_impl` 使用。JSONB 接受并忽略该参数。

        Args:
            python_type: 承载此列的 Pydantic 类型(可为 BaseModel 子类、
                list[BaseModel]、RootModel 子类等任意 Pydantic v2 支持的类型)。
        """
        super().__init__(python_type)
        self._adapter: TypeAdapter = TypeAdapter(python_type)

    def process_bind_param(self, value, dialect):  # noqa: ARG002
        """把 Pydantic 实例序列化为 JSON 兼容 dict,准备写入 DB。

        Args:
            value: 来自 ORM 赋值的 Pydantic 实例(允许为 None)。
            dialect: SQLAlchemy 方言实例,本装饰器不依赖方言特定行为。

        Returns:
            None 或 JSON 兼容的 dict。
        """
        if value is None:
            return None
        return self._adapter.dump_python(value, mode="json", warnings="error")

    def process_result_value(self, value, dialect):  # noqa: ARG002
        """把 DB 读出的 dict 还原为 Pydantic 实例。

        Args:
            value: DB 返回的 dict(允许为 None)。
            dialect: SQLAlchemy 方言实例,本装饰器不依赖方言特定行为。

        Returns:
            None 或 Pydantic 实例。Pydantic ValidationError 透传。
        """
        if value is None:
            return None
        return self._adapter.validate_python(value)
