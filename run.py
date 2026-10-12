"""
Главная точка входа для хостинга (Bothost).
Одновременно запускает Discord-бота и веб-панель FastAPI (Uvicorn) в одном процессе.
С принудительным сбросом буфера вывода (flush) и подробным логированием.
"""

from __future__ import annotations

import os
import sys

# 1. Принудительно отключаем буферизацию вывода Python (для логов Docker/Bothost)
os.environ["PYTHONUNBUFFERED"] = "1"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

print("=" * 60, flush=True)
print("🚀 [Bothost] Запуск контейнера Discord-модератора...", flush=True)
print(f"📦 Версия Python: {sys.version.split()[0]} | Рабочая папка: {os.getcwd()}", flush=True)
print("=" * 60, flush=True)

import asyncio
import logging
import uvicorn
from dotenv import load_dotenv

load_dotenv()

# Настройка логирования в стандартный поток вывода с немедленным flush
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

logging.root.setLevel(getattr(logging, log_level, logging.INFO))
logging.root.handlers = [handler]

log = logging.getLogger("entrypoint")


async def main():
    token = os.environ.get("DISCORD_TOKEN", "").strip()

    if not token or token == "your_bot_token_here":
        print("\n" + "!" * 64, file=sys.stderr, flush=True)
        print("❌ ОШИБКА: Переменная DISCORD_TOKEN не найдена или пуста!", file=sys.stderr, flush=True)
        print("Пожалуйста, перейдите в панель Bothost ➔ Environment Variables", file=sys.stderr, flush=True)
        print("и добавьте ключ: DISCORD_TOKEN=<токен вашего бота>", file=sys.stderr, flush=True)
        print("А также: GUILD_ID=1558606862277017690", file=sys.stderr, flush=True)
        print("!" * 64 + "\n", file=sys.stderr, flush=True)
        
        # Ждём, чтобы процесс не падал мгновенно в циклический перезапуск
        while True:
            await asyncio.sleep(30)

    from bot.database import init_db, DATABASE_URL
    from bot.config_cache import config_cache
    from bot.main import ModerationBot
    from web.app import app

    log.info(f"Подключение к БД: {DATABASE_URL}")

    # 1. Инициализация базы данных (с таймаутом, чтобы не зависать при медленной сети)
    try:
        log.info("⏳ Проверка подключения к базе данных...")
        await asyncio.wait_for(init_db(), timeout=15.0)
        log.info("✅ База данных успешно инициализирована")
    except asyncio.TimeoutError:
        log.warning("⚠️ Таймаут подключения к БД (15 сек). Продолжаем запуск бота...")
    except Exception as e:
        log.error(f"⚠️ Ошибка инициализации БД ({e}). Продолжаем запуск...", exc_info=True)

    # 2. Настройка порта и хоста веб-панели (поддержка Bothost PORT)
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("WEB_PORT") or 8000)

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    log.info(f"🌐 Веб-панель запускается на {host}:{port} (URL: https://mod.ovrdose.bothost.tech)")

    # 3. Инициализация Discord-бота
    bot = ModerationBot()
    log.info("🤖 Подключение Discord-бота к Gateway...")

    try:
        await asyncio.gather(
            server.serve(),
            bot.start(token),
        )
    except Exception as e:
        if "PrivilegedIntentsRequired" in str(type(e)) or "PrivilegedIntents" in str(e):
            print("\n" + "!" * 64, file=sys.stderr, flush=True)
            print("❌ ОШИБКА ДИСКОРДА: PrivilegedIntentsRequired!", file=sys.stderr, flush=True)
            print("Вам необходимо перейти в Discord Developer Portal ➔ Bot", file=sys.stderr, flush=True)
            print("и включить переключатель: [SERVER MEMBERS INTENT]", file=sys.stderr, flush=True)
            print("!" * 64 + "\n", file=sys.stderr, flush=True)
        else:
            log.error(f"Непредвиденная ошибка в основном цикле: {e}", exc_info=True)
    finally:
        await config_cache.close()
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
