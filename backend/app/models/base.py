"""DeclarativeBase 与公共 Mixin（复用 kb-platform 模式，SQLite 兼容主键）。"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())


class CreatedMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


IntPK = int  # 主键统一 Integer（SQLite 自增兼容；MySQL INT 足够）
_ = Integer, mapped_column, func, datetime, CreatedMixin  # re-export hints
