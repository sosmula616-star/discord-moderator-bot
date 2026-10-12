"""
Пул соединений к базе данных PostgreSQL (asyncpg) + вспомогательные функции.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from dotenv import load_dotenv

load_dotenv()

raw_url = os.environ.get("DATABASE_URL", "").strip()

# Приводим к формату asyncpg для асинхронного драйвера PostgreSQL
if raw_url.startswith("postgresql://"):
    DATABASE_URL = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
elif raw_url.startswith("postgres://"):
    DATABASE_URL = raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
else:
    DATABASE_URL = raw_url

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

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
    """Создаёт таблицы в базе данных PostgreSQL (если ещё не созданы)."""
    from bot.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
