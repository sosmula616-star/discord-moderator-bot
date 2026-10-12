"""
Главная точка входа для хостинга (Bothost).
Одновременно запускает Discord-бота и веб-панель FastAPI (Uvicorn) в одном процессе.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uvicorn
from dotenv import load_dotenv

from bot.database import init_db
from bot.config_cache import config_cache
from bot.main import ModerationBot
from web.app import app

load_dotenv()

# Логирование
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("entrypoint")


async def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN не задан в .env")

    # Инициализация схемы БД
    await init_db()

    # Настройка Uvicorn для веб-панели (mod.ovrdose.bothost.tech)
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", 8000))
    
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Инициализация Discord-бота
    bot = ModerationBot()

    log.info(f"Запуск веб-панели на {host}:{port} и Discord-бота...")

    try:
        await asyncio.gather(
            server.serve(),
            bot.start(token),
        )
    finally:
        await config_cache.close()
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
