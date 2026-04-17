from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。M2 中各模块模型继承此类。"""
    pass
