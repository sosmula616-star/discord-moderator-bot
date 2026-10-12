"""
Пул соединений к базе данных (SQLite / PostgreSQL) + вспомогательные функции.
Поддерживает общую БД с Root (SQLite в WAL-режиме) или отдельный PostgreSQL.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "sqlite+aiosqlite:///../Root ovrdose/root-core/data/shared.db"
)

is_sqlite = "sqlite" in DATABASE_URL

engine_kwargs = {"echo": False}
if is_sqlite:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    engine_kwargs.update({
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,
    })

engine = create_async_engine(DATABASE_URL, **engine_kwargs)

if is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Контекстный менеджер сессии."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Создаёт таблицы в базе данных (если ещё не созданы)."""
    from bot.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
