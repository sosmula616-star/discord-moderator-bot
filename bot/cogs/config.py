"""
Cog: Config — настройка сервера при первом запуске бота.
Создаёт роли, каналы, категории, проверяет права.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands
from sqlalchemy import select

from bot.config_cache import config_cache, ensure_default_buttons
from bot.database import AsyncSessionLocal
from bot.models import GuildSettings
from bot.utils.punishments import (
    ensure_guild_roles, sync_ban_role_permissions, sync_mute_permissions
)

log = logging.getLogger(__name__)

REQUIRED_PERMISSIONS = [
    ("manage_roles", "Управление ролями"),
    ("manage_channels", "Управление каналами"),
    ("move_members", "Перемещение участников"),
    ("view_audit_log", "Просмотр журнала аудита"),
    ("moderate_members", "Тайм-аут участников"),
    ("manage_messages", "Управление сообщениями"),
]


async def check_bot_permissions(guild: discord.Guild, bot_member: discord.Member) -> list[str]:
    """Проверяет разрешения бота. Возвращает список отсутствующих."""
    missing = []
    perms = bot_member.guild_permissions
    for perm_attr, label in REQUIRED_PERMISSIONS:
        if not getattr(perms, perm_attr, False):
            missing.append(label)
    return missing


async def check_role_hierarchy(guild: discord.Guild, bot_member: discord.Member, settings: GuildSettings) -> list[str]:
    """Проверяет иерархию ролей. Возвращает список проблем."""
    problems = []
    bot_top = bot_member.top_role

    managed_role_ids = [
        settings.role_access_id, settings.role_noaccess_id,
        settings.role_male_id, settings.role_female_id,
        settings.role_ban_id, settings.role_mute_voice_id, settings.role_mute_text_id,
        settings.role_warn1_id, settings.role_warn2_id, settings.role_warning_id,
    ]
    for role_id in managed_role_ids:
        if not role_id:
            continue
        role = guild.get_role(role_id)
        if role and role >= bot_top:
            problems.append(f"Роль «{role.name}» ({role.id}) >= роли бота")
    return problems


async def setup_guild(guild: discord.Guild, bot: discord.Client) -> None:
    """
    Полная инициализация сервера:
    1. Создать роли если нет
    2. Создать каналы лога и бан-комнату если нет
    3. Синхронизировать права
    """
    bot_member = guild.get_member(bot.user.id)
    if not bot_member:
        return

    # Получаем/создаём настройки
    async with AsyncSessionLocal() as session:
        settings = await session.get(GuildSettings, guild.id)
        if settings is None:
            settings = GuildSettings(guild_id=guild.id)
            session.add(settings)
            await session.commit()

    # Проверка прав
    missing_perms = await check_bot_permissions(guild, bot_member)
    if missing_perms:
        log.warning(f"[{guild.name}] Отсутствующие права бота: {', '.join(missing_perms)}")

    # Создаём роли
    await ensure_guild_roles(guild, settings)

    # Перечитываем settings после создания ролей
    async with AsyncSessionLocal() as session:
        settings = await session.get(GuildSettings, guild.id)

    # Кнопки по умолчанию
    await ensure_default_buttons(guild.id)

    # Проверка иерархии
    problems = await check_role_hierarchy(guild, bot_member, settings)
    if problems:
        for p in problems:
            log.warning(f"[{guild.name}] Иерархия ролей: {p}")

    # Категория логов
    await _ensure_logs_category(guild, settings)

    # Бан-комната
    await _ensure_ban_room(guild, settings, bot)

    # Синхронизация прав бана и мутов
    ban_role = guild.get_role(settings.role_ban_id) if settings.role_ban_id else None
    mute_voice = guild.get_role(settings.role_mute_voice_id) if settings.role_mute_voice_id else None
    mute_text = guild.get_role(settings.role_mute_text_id) if settings.role_mute_text_id else None

    if ban_role:
        await sync_ban_role_permissions(guild, ban_role)
    if mute_voice or mute_text:
        await sync_mute_permissions(guild, mute_voice, mute_text)

    # Обновляем кэш
    await config_cache.load(guild.id)
    log.info(f"[{guild.name}] Настройка сервера завершена")


async def _ensure_logs_category(guild: discord.Guild, settings: GuildSettings) -> None:
    """Создаёт категорию «Логи» и каналы внутри неё, если их нет."""
    # Права: только администраторы
    admin_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    # Добавляем администраторов
    for role in guild.roles:
        if role.permissions.administrator:
            admin_overwrites[role] = discord.PermissionOverwrite(view_channel=True)

    changed = False
    async with AsyncSessionLocal() as session:
        s = await session.get(GuildSettings, guild.id)

        # Категория
        category = guild.get_channel(s.category_logs_id) if s.category_logs_id else None
        if category is None:
            try:
                # Размещаем в конце
                position = len(guild.channels)
                category = await guild.create_category(
                    name="Логи",
                    overwrites=admin_overwrites,
                    reason="Автоматическое создание категории логов",
                )
                s.category_logs_id = category.id
                changed = True
            except (discord.Forbidden, discord.HTTPException) as e:
                log.error(f"_ensure_logs_category: не удалось создать категорию: {e}")
                return

        # Каналы логов
        log_channels = [
            ("channel_log_reports_id",  "логи-репорты"),
            ("channel_log_punish_id",   "логи-наказания"),
            ("channel_log_verify_id",   "логи-верификация"),
            ("channel_log_panel_id",    "логи-панель"),
        ]
        for attr, name in log_channels:
            channel_id = getattr(s, attr)
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is None:
                try:
                    channel = await guild.create_text_channel(
                        name=name,
                        category=category,
                        overwrites=admin_overwrites,
                        reason=f"Автоматическое создание канала {name}",
                    )
                    setattr(s, attr, channel.id)
                    changed = True
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.error(f"_ensure_logs_category: не удалось создать канал {name}: {e}")

        if changed:
            await session.commit()


async def _ensure_ban_room(
    guild: discord.Guild, settings: GuildSettings, bot: discord.Client
) -> None:
    """Создаёт бан-комнату, если её нет."""
    if settings.channel_ban_room_id and guild.get_channel(settings.channel_ban_room_id):
        return

    ban_role = guild.get_role(settings.role_ban_id) if settings.role_ban_id else None
    if not ban_role:
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
        ban_role: discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=False,
        ),
    }
    try:
        channel = await guild.create_text_channel(
            name="бан-комната",
            overwrites=overwrites,
            topic="Информация о вашем бане. Писать нельзя.",
            reason="Автоматическое создание бан-комнаты",
        )
        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            if s:
                s.channel_ban_room_id = channel.id
            await session.commit()
        log.info(f"[{guild.name}] Бан-комната создана: {channel.id}")

        # Закрепляем сообщение
        embed = discord.Embed(
            title="🔨 Вы заблокированы на сервере",
            description=(
                "Вы получили **бан (роль)**. Вам недоступны другие каналы.\n\n"
                "Причина и срок бана будут указаны в личных сообщениях от бота.\n"
                "Дождитесь истечения срока или обратитесь к администрации."
            ),
            color=discord.Color.dark_red(),
        )
        msg = await channel.send(embed=embed)
        try:
            await msg.pin()
        except Exception:
            pass
    except (discord.Forbidden, discord.HTTPException) as e:
        log.error(f"_ensure_ban_room: не удалось создать бан-комнату: {e}")


class ConfigCog(commands.Cog, name="Config"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """Запускается при готовности бота. Инициализирует все серверы."""
        log.info(f"Бот запущен: {self.bot.user} ({self.bot.user.id})")
        log.info(f"Серверов: {len(self.bot.guilds)}")

        for guild in self.bot.guilds:
            try:
                await setup_guild(guild, self.bot)
            except Exception as e:
                log.error(f"[{guild.name}] Ошибка инициализации: {e}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Инициализация при добавлении бота на новый сервер."""
        log.info(f"Добавлен на сервер: {guild.name} ({guild.id})")
        try:
            await setup_guild(guild, self.bot)
        except Exception as e:
            log.error(f"[{guild.name}] Ошибка инициализации: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigCog(bot))
