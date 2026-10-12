"""
Кэш конфигурации сервера. Загружается из БД при старте и при получении
PostgreSQL NOTIFY 'config_update'. Остальные модули читают из этого кэша,
не обращаясь к БД на каждый запрос.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

import asyncpg
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload

from bot.database import AsyncSessionLocal
from bot.models import ButtonConfig, ButtonRole, GuildSettings, PanelAccess

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class GuildConfig:
    """Конфигурация одного сервера."""

    def __init__(self, settings: GuildSettings, buttons: list[ButtonConfig]):
        self.guild_id = settings.guild_id

        # Роли верификации
        self.role_access_id   = settings.role_access_id
        self.role_noaccess_id = settings.role_noaccess_id
        self.role_male_id     = settings.role_male_id
        self.role_female_id   = settings.role_female_id

        # Роли наказаний
        self.role_ban_id        = settings.role_ban_id
        self.role_mute_voice_id = settings.role_mute_voice_id
        self.role_mute_text_id  = settings.role_mute_text_id
        self.role_warn1_id      = settings.role_warn1_id
        self.role_warn2_id      = settings.role_warn2_id
        self.role_warning_id    = settings.role_warning_id

        # Каналы
        self.channel_ban_room_id    = settings.channel_ban_room_id
        self.channel_reports_id     = settings.channel_reports_id
        self.category_reports_id    = settings.category_reports_id
        self.category_logs_id       = settings.category_logs_id
        self.channel_log_reports_id = settings.channel_log_reports_id
        self.channel_log_punish_id  = settings.channel_log_punish_id
        self.channel_log_verify_id  = settings.channel_log_verify_id
        self.channel_log_panel_id   = settings.channel_log_panel_id

        # Лимиты
        self.max_mute_minutes      = settings.max_mute_minutes
        self.auto_ban_warn_count   = settings.auto_ban_warn_count
        self.auto_ban_days         = settings.auto_ban_days
        self.report_limit_per_hour = settings.report_limit_per_hour

        # Кнопки: {action_key: {label, emoji, style, order, enabled, role_ids: []}}
        self.buttons: Dict[str, Dict[str, Any]] = {}
        for btn in buttons:
            self.buttons[btn.action_key] = {
                "id":       btn.id,
                "label":    btn.label,
                "emoji":    btn.emoji,
                "style":    btn.style.value,
                "order":    btn.order,
                "enabled":  btn.enabled,
                "role_ids": [r.role_id for r in btn.roles],
            }


class ConfigCache:
    """Потокобезопасный кэш конфигурации всех серверов."""

    def __init__(self) -> None:
        self._cache: Dict[int, GuildConfig] = {}
        self._lock = asyncio.Lock()
        self._listen_conn: Optional[asyncpg.Connection] = None

    async def load(self, guild_id: int) -> GuildConfig:
        """Загрузить/обновить конфиг из БД."""
        async with AsyncSessionLocal() as session:
            stmt = select(GuildSettings).where(GuildSettings.guild_id == guild_id)
            result = await session.execute(stmt)
            settings = result.scalar_one_or_none()

            if settings is None:
                # Создать запись по умолчанию
                settings = GuildSettings(guild_id=guild_id)
                session.add(settings)
                await session.commit()

            btn_stmt = (
                select(ButtonConfig)
                .where(ButtonConfig.guild_id == guild_id)
                .options(selectinload(ButtonConfig.roles))
                .order_by(ButtonConfig.order)
            )
            btn_result = await session.execute(btn_stmt)
            buttons = btn_result.scalars().all()

            cfg = GuildConfig(settings, list(buttons))

        async with self._lock:
            self._cache[guild_id] = cfg
        return cfg

    def get(self, guild_id: int) -> Optional[GuildConfig]:
        return self._cache.get(guild_id)

    async def get_or_load(self, guild_id: int) -> GuildConfig:
        cfg = self.get(guild_id)
        if cfg is None:
            cfg = await self.load(guild_id)
        return cfg

    async def invalidate(self, guild_id: int) -> None:
        async with self._lock:
            self._cache.pop(guild_id, None)

    async def start_listener(self, dsn: str) -> None:
        """
        Подключается к PostgreSQL и слушает канал 'config_update'.
        Веб-панель вызывает NOTIFY config_update, '{"guild_id": 123}'.
        """
        try:
            self._listen_conn = await asyncpg.connect(dsn=dsn)
            await self._listen_conn.add_listener("config_update", self._on_notify)
            log.info("ConfigCache: слушаю канал config_update")
        except Exception as e:
            log.error(f"ConfigCache: не удалось подключиться к LISTEN: {e}")

    async def _on_notify(self, conn: Any, pid: Any, channel: Any, payload: str) -> None:
        try:
            data = json.loads(payload)
            guild_id = int(data["guild_id"])
            log.info(f"ConfigCache: получен NOTIFY для guild {guild_id}, перезагружаю конфиг")
            await self.load(guild_id)
        except Exception as e:
            log.error(f"ConfigCache._on_notify: {e}")

    async def close(self) -> None:
        if self._listen_conn:
            await self._listen_conn.close()


# Глобальный экземпляр
config_cache = ConfigCache()


async def ensure_default_buttons(guild_id: int) -> None:
    """Создаёт строки button_config по умолчанию, если их ещё нет."""
    DEFAULT_BUTTONS = [
        ("verify_access",   "✅ Выдать допуск",     None, "success",   0),
        ("verify_noaccess", "🚫 Недопуск",           None, "danger",    1),
        ("verify_male",     "♂ Мужской",             None, "primary",   2),
        ("verify_female",   "♀ Женский",             None, "primary",   3),
        ("ban",             "🔨 Бан",                None, "danger",    4),
        ("mute_voice",      "🔇 Голос. мут",         None, "secondary", 5),
        ("mute_text",       "✏️ Текст. мут",          None, "secondary", 6),
        ("warn",            "⚠️ Варн",                None, "secondary", 7),
        ("warning",         "🔔 Предупреждение",     None, "secondary", 8),
        ("reports_view",    "📋 Жалобы",             None, "secondary", 9),
        ("history",         "📜 История",            None, "secondary", 10),
        ("revoke",          "🗑️ Снять наказание",    None, "danger",    11),
    ]

    async with AsyncSessionLocal() as session:
        for action_key, label, emoji, style, order in DEFAULT_BUTTONS:
            stmt = select(ButtonConfig).where(
                ButtonConfig.guild_id == guild_id,
                ButtonConfig.action_key == action_key,
            )
            existing = await session.execute(stmt)
            if existing.scalar_one_or_none() is None:
                btn = ButtonConfig(
                    guild_id=guild_id,
                    action_key=action_key,
                    label=label,
                    emoji=emoji,
                    style=ButtonStyle(style),  # type: ignore
                    order=order,
                    enabled=True,
                )
                session.add(btn)
        await session.commit()


from bot.models import ButtonStyle  # noqa: E402 (круговой импорт, размещён в конце)
