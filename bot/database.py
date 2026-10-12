"""
Пул соединений к базе данных (SQLite / PostgreSQL) + вспомогательные функции.
Поддерживает общую БД с Root (SQLite в WAL-режиме) или отдельный PostgreSQL.
Автоматически создаёт папки и гарантирует запуск на хостинге (Bothost).
"""

from __future__ import annotations

import os
import sys
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

raw_url = os.environ.get("DATABASE_URL", "").strip()

if not raw_url:
    # Проверяем общую папку Root ovrdose
    shared_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../Root ovrdose/root-core/data/shared.db")
    )
    if os.path.exists(os.path.dirname(shared_root)):
        db_file = shared_root
    else:
        # Локальная папка для Bothost / Docker
        local_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data"))
        os.makedirs(local_dir, exist_ok=True)
        db_file = os.path.join(local_dir, "shared.db")
else:
    # Если передали postgresql:// или postgres://, конвертируем в asyncpg
    if raw_url.startswith("postgresql://"):
        DATABASE_URL = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif raw_url.startswith("postgres://"):
        DATABASE_URL = raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
    else:
        DATABASE_URL = raw_url

is_sqlite = "sqlite" in DATABASE_URL

# Если SQLite, гарантируем, что родительская папка для файла существует
if is_sqlite:
    try:
        clean_path = DATABASE_URL.split(":///")[-1]
        parent_dir = os.path.dirname(os.path.abspath(clean_path))
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
    except Exception as e:
        print(f"[Database] Внимание: не удалось создать папку для SQLite: {e}", file=sys.stderr, flush=True)
        # Резервный локальный путь
        fallback_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data"))
        os.makedirs(fallback_dir, exist_ok=True)
        DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(fallback_dir, 'shared.db').replace(os.sep, '/')}"

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
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
        finally:
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
    """Создаёт таблицы в базе данных (если ещё не созданы) и импортирует схему Root."""
    from bot.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        if is_sqlite:
            schema_file = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "../data/shared_schema.sql")
            )
            if os.path.exists(schema_file):
                with open(schema_file, "r", encoding="utf-8") as f:
                    sql_script = f.read()

                def apply_sql(dbapi_conn):
                    cursor = dbapi_conn.cursor()
                    try:
                        cursor.executescript(sql_script)
                    finally:
                        cursor.close()

                await conn.run_sync(apply_sql)
