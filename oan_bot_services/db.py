"""Engine and sessions. Parameters are hidden from SQLAlchemy errors, so a database
exception never carries a chat id, Farmer ID or phone into a log."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(url: str, *, pool_size: int = 5, max_overflow: int = 5) -> AsyncEngine:
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("database URL must use the postgresql+psycopg driver")
    return create_async_engine(
        url, pool_size=pool_size, max_overflow=max_overflow, pool_pre_ping=True,
        hide_parameters=True, echo=False,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
