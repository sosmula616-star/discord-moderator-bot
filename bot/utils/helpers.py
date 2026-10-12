"""
Вспомогательные функции: логирование в Discord-каналы и аудит-лог.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from sqlalchemy import insert

from bot.database import AsyncSessionLocal
from bot.models import AuditLog

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def format_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return f"<t:{int(dt.timestamp())}:F>"


def format_duration(seconds: int) -> str:
    """Форматирует длительность в человекочитаемую строку."""
    if seconds < 60:
        return f"{seconds} сек."
    if seconds < 3600:
        return f"{seconds // 60} мин."
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h} ч. {m} мин." if m else f"{h} ч."
    d = seconds // 86400
    return f"{d} дн."


async def audit(
    guild_id: int,
    action: str,
    actor_id: Optional[int] = None,
    target_id: Optional[int] = None,
    punishment_id: Optional[int] = None,
    report_id: Optional[int] = None,
    details: Optional[str] = None,
) -> None:
    """Записывает событие в таблицу audit_log."""
    try:
        async with AsyncSessionLocal() as session:
            session.add(AuditLog(
                guild_id=guild_id,
                action=action,
                actor_id=actor_id,
                target_id=target_id,
                punishment_id=punishment_id,
                report_id=report_id,
                details=details,
            ))
            await session.commit()
    except Exception as e:
        log.error(f"audit(): ошибка записи в audit_log: {e}")


async def send_log(
    bot: discord.Client,
    channel_id: Optional[int],
    embed: discord.Embed,
) -> None:
    """Отправляет embed в лог-канал. Ошибки игнорирует."""
    if not channel_id:
        return
    try:
        channel = bot.get_channel(channel_id)
        if channel and isinstance(channel, discord.TextChannel):
            await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"send_log: не удалось отправить лог в канал {channel_id}: {e}")


def punishment_embed(
    title: str,
    color: discord.Color,
    moderator: Optional[discord.Member | discord.User] = None,
    target: Optional[discord.Member | discord.User] = None,
    reason: Optional[str] = None,
    description: Optional[str] = None,
    duration: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    extra_fields: Optional[list[tuple[str, str, bool]]] = None,
    report_id: Optional[int] = None,
    auto: bool = False,
) -> discord.Embed:
    """Строит стандартный embed для лог-каналов."""
    embed = discord.Embed(title=title, color=color, timestamp=utcnow())

    if auto:
        embed.add_field(name="⚙️ Тип", value="Автоматическое", inline=True)

    if moderator:
        embed.add_field(name="👮 Модератор", value=moderator.mention, inline=True)

    if target:
        embed.add_field(name="🎯 Цель", value=f"{target.mention} (`{target.id}`)", inline=True)

    if reason:
        embed.add_field(name="📝 Причина", value=reason, inline=False)

    if description:
        embed.add_field(name="📄 Описание", value=description[:1024], inline=False)

    if duration:
        embed.add_field(name="⏱️ Длительность", value=duration, inline=True)

    if expires_at:
        embed.add_field(name="⌛ Истекает", value=format_dt(expires_at), inline=True)

    if report_id:
        embed.add_field(name="📋 Репорт", value=f"#{report_id}", inline=True)

    if extra_fields:
        for name, value, inline in extra_fields:
            embed.add_field(name=name, value=value, inline=inline)

    embed.set_footer(text=f"ID цели: {target.id}" if target else "")
    return embed


async def safe_dm(
    user: discord.User | discord.Member,
    embed: discord.Embed,
) -> None:
    """Отправляет DM пользователю. Игнорирует ошибки закрытых ЛС."""
    try:
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass
