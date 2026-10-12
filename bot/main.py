"""
Точка входа бота. Запуск: python -m bot.main
"""

from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from bot.database import init_db
from bot.config_cache import config_cache

load_dotenv()

# ─────────────────────────── Логирование ───────────────────────────
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────── Bot ───────────────────────────
INTENTS = discord.Intents.default()
INTENTS.members = True          # для on_member_join/update/remove
INTENTS.voice_states = True     # для голосовых каналов
# message_content не нужен

COGS = [
    "bot.cogs.config",
    "bot.cogs.scheduler",
    "bot.cogs.act",
    "bot.cogs.report",
]


class ModerationBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",  # не используется, только для инициализации
            intents=INTENTS,
            help_command=None,
        )
        self.guild_id = int(os.environ.get("GUILD_ID", 1558606862277017690))

    async def on_ready(self):
        log.info(f"✅ Discord-бот успешно запущен как {self.user} (ID: {self.user.id})")
        log.info(f"Бот присутствует на {len(self.guilds)} серверах")

    async def setup_hook(self):
        # Загружаем коги
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info(f"Загружен cog: {cog}")
            except Exception as e:
                log.error(f"Ошибка загрузки cog {cog}: {e}", exc_info=True)

        # Синхронизируем slash-команды
        try:
            guild_obj = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            log.info(f"Slash-команды синхронизированы для guild {self.guild_id}")
        except Exception as e:
            log.warning(f"Не удалось сразу синхронизировать команды: {e}")

    async def close(self):
        await config_cache.close()
        await super().close()


async def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN не задан в .env")

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL не задан в .env")

    # Инициализируем таблицы базы данных
    await init_db()

    bot = ModerationBot()
    async with bot:
        # Для PostgreSQL запускаем NOTIFY listener параллельно
        if "postgresql" in database_url:
            pg_dsn = database_url.replace("postgresql+asyncpg://", "postgresql://")
            asyncio.ensure_future(config_cache.start_listener(pg_dsn))
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
