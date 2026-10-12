"""
Логика выдачи и снятия наказаний (используется из cog act, scheduler, report).
Вынесена в отдельный модуль для переиспользования и тестируемости.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, TYPE_CHECKING

import discord
from sqlalchemy import select, func, and_

from bot.database import AsyncSessionLocal
from bot.models import (
    Punishment, PunishmentType, RoleBackup, User, GuildSettings
)
from bot.utils.helpers import audit, safe_dm, punishment_embed, utcnow, format_duration

if TYPE_CHECKING:
    from bot.config_cache import GuildConfig

log = logging.getLogger(__name__)


# ─────────────────────────── Role helpers ───────────────────────────

async def _get_or_create_role(
    guild: discord.Guild,
    name: str,
    color: discord.Color = discord.Color.default(),
    permissions: discord.Permissions = discord.Permissions.none(),
    reason: str = "Автоматическое создание роли ботом",
) -> discord.Role:
    role = discord.utils.get(guild.roles, name=name)
    if role is None:
        role = await guild.create_role(name=name, color=color, permissions=permissions, reason=reason)
    return role


async def ensure_guild_roles(
    guild: discord.Guild,
    settings: GuildSettings,
) -> None:
    """
    Создаёт необходимые роли, если они ещё не существуют.
    Сохраняет их ID в GuildSettings (БД).
    """
    from bot.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        db_settings = await session.get(GuildSettings, guild.id)
        if db_settings is None:
            db_settings = GuildSettings(guild_id=guild.id)
            session.add(db_settings)

        changed = False

        async def _ensure(attr: str, name: str, color: discord.Color = discord.Color.default()):
            nonlocal changed
            current_id = getattr(db_settings, attr)
            if current_id and guild.get_role(current_id):
                return  # роль уже есть
            role = await _get_or_create_role(guild, name, color)
            setattr(db_settings, attr, role.id)
            changed = True
            log.info(f"ensure_guild_roles: создана роль «{name}» ({role.id})")

        await _ensure("role_access_id",   "Допуск")
        await _ensure("role_noaccess_id", "Недопуск")
        await _ensure("role_male_id",     "♂ Мужской",  discord.Color.blue())
        await _ensure("role_female_id",   "♀ Женский",  discord.Color(0xFFB6C1))
        await _ensure("role_ban_id",      "Бан",         discord.Color.dark_red())
        await _ensure("role_mute_voice_id", "Мут Голос")
        await _ensure("role_mute_text_id",  "Мут Текст")
        await _ensure("role_warn1_id",    "Варн 1/3",   discord.Color.orange())
        await _ensure("role_warn2_id",    "Варн 2/3",   discord.Color.red())
        await _ensure("role_warning_id",  "Предупреждение", discord.Color.yellow())

        if changed:
            await session.commit()


async def sync_ban_role_permissions(guild: discord.Guild, ban_role: discord.Role) -> None:
    """
    Устанавливает overrides роли «Бан»: запрет на всё везде,
    кроме бан-комнаты (определяется по имени канала «бан-комната»).
    """
    deny_all = discord.PermissionOverwrite(
        view_channel=False,
        connect=False,
        send_messages=False,
        speak=False,
        read_message_history=False,
        add_reactions=False,
        send_messages_in_threads=False,
        create_public_threads=False,
        create_private_threads=False,
    )
    allow_room = discord.PermissionOverwrite(
        view_channel=True,
        read_message_history=True,
    )

    ban_room_channel = discord.utils.get(guild.channels, name="бан-комната")

    for channel in guild.channels:
        try:
            if channel == ban_room_channel:
                await channel.set_permissions(ban_role, overwrite=allow_room, reason="Синхронизация прав бана")
            else:
                await channel.set_permissions(ban_role, overwrite=deny_all, reason="Синхронизация прав бана")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"sync_ban_role_permissions: не удалось выставить права на канале {channel.name}: {e}")


async def sync_mute_permissions(
    guild: discord.Guild,
    mute_voice_role: Optional[discord.Role],
    mute_text_role: Optional[discord.Role],
) -> None:
    """Устанавливает overrides ролей мутов на всех каналах."""
    for channel in guild.channels:
        try:
            if mute_voice_role and isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                await channel.set_permissions(
                    mute_voice_role,
                    overwrite=discord.PermissionOverwrite(connect=False, speak=False),
                    reason="Синхронизация прав мута"
                )
            if mute_text_role and isinstance(channel, discord.TextChannel):
                await channel.set_permissions(
                    mute_text_role,
                    overwrite=discord.PermissionOverwrite(
                        send_messages=False,
                        add_reactions=False,
                        create_public_threads=False,
                        create_private_threads=False,
                        send_messages_in_threads=False,
                    ),
                    reason="Синхронизация прав мута"
                )
        except (discord.Forbidden, discord.HTTPException):
            pass


# ─────────────────────────── User record ───────────────────────────

async def upsert_user(member: discord.Member) -> None:
    """Обновляет или создаёт запись пользователя в БД."""
    async with AsyncSessionLocal() as session:
        user = await session.get(User, member.id)
        if user is None:
            user = User(
                id=member.id,
                guild_id=member.guild.id,
                username=str(member),
                display_name=member.display_name,
                avatar_url=str(member.display_avatar.url) if member.display_avatar else None,
                first_seen=member.joined_at or utcnow(),
                joined_at=member.joined_at,
            )
            session.add(user)
        else:
            user.username = str(member)
            user.display_name = member.display_name
            if member.display_avatar:
                user.avatar_url = str(member.display_avatar.url)
            user.joined_at = member.joined_at
            user.left_at = None
        await session.commit()


# ─────────────────────────── Ban logic ───────────────────────────

async def apply_ban(
    guild: discord.Guild,
    target: discord.Member,
    moderator_id: int,
    reason: str,
    description: str,
    expires_at: Optional[datetime],
    cfg,
    auto: bool = False,
) -> Punishment:
    """
    Выдаёт бан-роль: сохраняет бэкап ролей → снимает все роли → выдаёт «Бан».
    Возвращает объект Punishment.
    """
    ban_role = guild.get_role(cfg.role_ban_id) if cfg.role_ban_id else None
    if not ban_role:
        raise ValueError("Роль «Бан» не найдена. Настройте её в панели.")

    # Управляемые/интеграционные роли — пропускаем
    roles_to_save = [
        r for r in target.roles
        if r != guild.default_role and not r.managed
    ]

    async with AsyncSessionLocal() as session:
        # Создаём запись наказания
        punishment = Punishment(
            guild_id=guild.id,
            user_id=target.id,
            type=PunishmentType.ban_role,
            moderator_id=moderator_id,
            reason=reason,
            description=description,
            expires_at=expires_at,
            active=True,
            auto=auto,
        )
        session.add(punishment)
        await session.flush()  # чтобы получить punishment.id

        # Сохраняем бэкап ролей
        for role in roles_to_save:
            session.add(RoleBackup(
                punishment_id=punishment.id,
                user_id=target.id,
                role_id=role.id,
            ))
        await session.commit()
        punishment_id = punishment.id

    # Убираем все роли и выдаём бан-роль
    try:
        await target.remove_roles(*roles_to_save, reason=f"Бан: {reason}")
    except discord.Forbidden:
        log.warning(f"apply_ban: нет прав убрать роли у {target.id}")
    except discord.HTTPException as e:
        log.error(f"apply_ban: ошибка снятия ролей: {e}")

    try:
        await target.add_roles(ban_role, reason=f"Бан: {reason}")
    except (discord.Forbidden, discord.HTTPException) as e:
        log.error(f"apply_ban: не удалось выдать роль Бан: {e}")

    # Выкидываем из голосового
    if target.voice:
        try:
            await target.move_to(None, reason="Бан")
        except (discord.Forbidden, discord.HTTPException):
            pass

    await audit(guild.id, "ban_apply", actor_id=moderator_id, target_id=target.id,
                punishment_id=punishment_id, details=reason)

    async with AsyncSessionLocal() as session:
        p = await session.get(Punishment, punishment_id)
        return p


async def revoke_ban(
    guild: discord.Guild,
    punishment: Punishment,
    revoked_by: int,
    revoke_reason: str,
    bot: discord.Client,
) -> list[int]:
    """
    Снимает бан-роль, возвращает сохранённые роли.
    Возвращает список ID ролей, которые не удалось вернуть.
    """
    cfg_from_db: GuildSettings
    async with AsyncSessionLocal() as session:
        cfg_from_db = await session.get(GuildSettings, guild.id)

    ban_role = guild.get_role(cfg_from_db.role_ban_id) if cfg_from_db and cfg_from_db.role_ban_id else None

    member = guild.get_member(punishment.user_id)
    failed_roles: list[int] = []

    if member:
        # Убираем бан-роль
        if ban_role and ban_role in member.roles:
            try:
                await member.remove_roles(ban_role, reason=f"Снятие бана: {revoke_reason}")
            except (discord.Forbidden, discord.HTTPException) as e:
                log.error(f"revoke_ban: не удалось снять роль Бан: {e}")

        # Возвращаем роли
        async with AsyncSessionLocal() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(RoleBackup).where(RoleBackup.punishment_id == punishment.id)
            )
            backups = result.scalars().all()

        bot_member = guild.get_member(bot.user.id)
        bot_top = bot_member.top_role if bot_member else None

        roles_to_return = []
        for backup in backups:
            role = guild.get_role(backup.role_id)
            if role is None:
                log.warning(f"revoke_ban: роль {backup.role_id} не найдена на сервере")
                failed_roles.append(backup.role_id)
                continue
            if bot_top and role >= bot_top:
                log.warning(f"revoke_ban: роль {role.name} выше роли бота, пропускаю")
                failed_roles.append(backup.role_id)
                continue
            roles_to_return.append(role)

        if roles_to_return:
            try:
                await member.add_roles(*roles_to_return, reason=f"Снятие бана: {revoke_reason}")
            except (discord.Forbidden, discord.HTTPException) as e:
                log.error(f"revoke_ban: не удалось вернуть роли: {e}")

    # Обновляем запись в БД
    async with AsyncSessionLocal() as session:
        p = await session.get(Punishment, punishment.id)
        if p:
            p.active = False
            p.revoked_by = revoked_by
            p.revoked_at = utcnow()
            p.revoke_reason = revoke_reason
            # Удаляем бэкап ролей
            from sqlalchemy import delete
            await session.execute(
                delete(RoleBackup).where(RoleBackup.punishment_id == punishment.id)
            )
            await session.commit()

    await audit(guild.id, "ban_revoke", actor_id=revoked_by,
                target_id=punishment.user_id, punishment_id=punishment.id,
                details=revoke_reason)
    return failed_roles


# ─────────────────────────── Mute logic ───────────────────────────

async def apply_mute(
    guild: discord.Guild,
    target: discord.Member,
    moderator_id: int,
    mute_type: str,  # "voice" или "text"
    reason: str,
    description: str,
    expires_at: datetime,
    cfg,
) -> Punishment:
    """Выдаёт мут-роль."""
    if mute_type == "voice":
        role_id = cfg.role_mute_voice_id
        p_type = PunishmentType.mute_voice
    else:
        role_id = cfg.role_mute_text_id
        p_type = PunishmentType.mute_text

    mute_role = guild.get_role(role_id) if role_id else None
    if not mute_role:
        raise ValueError(f"Роль мута ({mute_type}) не найдена. Настройте её в панели.")

    async with AsyncSessionLocal() as session:
        punishment = Punishment(
            guild_id=guild.id,
            user_id=target.id,
            type=p_type,
            moderator_id=moderator_id,
            reason=reason,
            description=description,
            expires_at=expires_at,
            active=True,
        )
        session.add(punishment)
        await session.commit()
        p_id = punishment.id

    try:
        await target.add_roles(mute_role, reason=f"Мут: {reason}")
    except (discord.Forbidden, discord.HTTPException) as e:
        log.error(f"apply_mute: {e}")

    if mute_type == "voice" and target.voice:
        try:
            await target.move_to(None, reason="Голосовой мут")
        except (discord.Forbidden, discord.HTTPException):
            pass

    await audit(guild.id, f"mute_{mute_type}_apply", actor_id=moderator_id,
                target_id=target.id, punishment_id=p_id, details=reason)

    async with AsyncSessionLocal() as session:
        return await session.get(Punishment, p_id)


async def revoke_mute(
    guild: discord.Guild,
    punishment: Punishment,
    revoked_by: int,
    revoke_reason: str,
    cfg,
) -> None:
    """Снимает мут-роль."""
    if punishment.type == PunishmentType.mute_voice:
        role_id = cfg.role_mute_voice_id
    else:
        role_id = cfg.role_mute_text_id

    role = guild.get_role(role_id) if role_id else None
    member = guild.get_member(punishment.user_id)

    if member and role and role in member.roles:
        # Снимаем только если нет другого активного мута того же типа
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Punishment).where(
                    Punishment.user_id == punishment.user_id,
                    Punishment.guild_id == guild.id,
                    Punishment.type == punishment.type,
                    Punishment.active == True,
                    Punishment.id != punishment.id,
                )
            )
            other_active = result.scalar_one_or_none()

        if not other_active:
            try:
                await member.remove_roles(role, reason=f"Снятие мута: {revoke_reason}")
            except (discord.Forbidden, discord.HTTPException) as e:
                log.error(f"revoke_mute: {e}")

    async with AsyncSessionLocal() as session:
        p = await session.get(Punishment, punishment.id)
        if p:
            p.active = False
            p.revoked_by = revoked_by
            p.revoked_at = utcnow()
            p.revoke_reason = revoke_reason
            await session.commit()

    await audit(guild.id, f"mute_revoke", actor_id=revoked_by,
                target_id=punishment.user_id, punishment_id=punishment.id,
                details=revoke_reason)


# ─────────────────────────── Warn logic ───────────────────────────

async def apply_warn(
    guild: discord.Guild,
    target: discord.Member,
    moderator_id: int,
    reason: str,
    description: str,
    cfg,
    bot: discord.Client,
) -> Punishment:
    """Выдаёт варн. При 3 варнах — авто-бан."""
    expires_at = utcnow() + timedelta(days=7)

    async with AsyncSessionLocal() as session:
        punishment = Punishment(
            guild_id=guild.id,
            user_id=target.id,
            type=PunishmentType.warn,
            moderator_id=moderator_id,
            reason=reason,
            description=description,
            expires_at=expires_at,
            active=True,
        )
        session.add(punishment)
        await session.commit()
        p_id = punishment.id

        # Считаем активные варны
        result = await session.execute(
            select(func.count()).where(
                Punishment.user_id == target.id,
                Punishment.guild_id == guild.id,
                Punishment.type == PunishmentType.warn,
                Punishment.active == True,
            )
        )
        warn_count = result.scalar_one()

    # Обновляем варн-роли
    await update_warn_roles(guild, target, warn_count, cfg)

    await audit(guild.id, "warn_apply", actor_id=moderator_id,
                target_id=target.id, punishment_id=p_id, details=reason)

    # Авто-бан при достижении cfg.auto_ban_warn_count варнов
    if warn_count >= cfg.auto_ban_warn_count:
        ban_expires = utcnow() + timedelta(days=cfg.auto_ban_days)
        try:
            await apply_ban(
                guild=guild,
                target=target,
                moderator_id=bot.user.id,
                reason=f"{cfg.auto_ban_warn_count} варна",
                description=f"Автоматический бан за достижение {cfg.auto_ban_warn_count} активных варнов.",
                expires_at=ban_expires,
                cfg=cfg,
                auto=True,
            )
            log.info(f"apply_warn: авто-бан выдан пользователю {target.id}")
        except Exception as e:
            log.error(f"apply_warn: не удалось выдать авто-бан: {e}")

    async with AsyncSessionLocal() as session:
        return await session.get(Punishment, p_id)


async def update_warn_roles(
    guild: discord.Guild,
    target: discord.Member,
    warn_count: int,
    cfg,
) -> None:
    """Обновляет роли варнов в зависимости от их количества."""
    role_warn1 = guild.get_role(cfg.role_warn1_id) if cfg.role_warn1_id else None
    role_warn2 = guild.get_role(cfg.role_warn2_id) if cfg.role_warn2_id else None

    to_add = []
    to_remove = []

    if warn_count >= 2 and role_warn2 and role_warn2 not in target.roles:
        to_add.append(role_warn2)
    elif warn_count < 2 and role_warn2 and role_warn2 in target.roles:
        to_remove.append(role_warn2)

    if warn_count >= 1 and role_warn1 and role_warn1 not in target.roles:
        to_add.append(role_warn1)
    elif warn_count == 0 and role_warn1 and role_warn1 in target.roles:
        to_remove.append(role_warn1)

    try:
        if to_add:
            await target.add_roles(*to_add, reason="Обновление варн-ролей")
        if to_remove:
            await target.remove_roles(*to_remove, reason="Обновление варн-ролей")
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"update_warn_roles: {e}")


# ─────────────────────────── Warning (краткое предупреждение) ───────────────────────────

async def apply_warning(
    guild: discord.Guild,
    target: discord.Member,
    moderator_id: int,
    reason: str,
    duration_hours: int,  # 1 или 6
    cfg,
) -> Punishment:
    """Выдаёт краткосрочное предупреждение (роль «Предупреждение»)."""
    expires_at = utcnow() + timedelta(hours=duration_hours)
    warning_role = guild.get_role(cfg.role_warning_id) if cfg.role_warning_id else None

    async with AsyncSessionLocal() as session:
        punishment = Punishment(
            guild_id=guild.id,
            user_id=target.id,
            type=PunishmentType.warning,
            moderator_id=moderator_id,
            reason=reason,
            expires_at=expires_at,
            active=True,
        )
        session.add(punishment)
        await session.commit()
        p_id = punishment.id

    if warning_role:
        try:
            await target.add_roles(warning_role, reason=f"Предупреждение: {reason}")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.error(f"apply_warning: {e}")

    await audit(guild.id, "warning_apply", actor_id=moderator_id,
                target_id=target.id, punishment_id=p_id, details=reason)

    async with AsyncSessionLocal() as session:
        return await session.get(Punishment, p_id)


# ─────────────────────────── Active punishments ───────────────────────────

async def get_active_punishments(user_id: int, guild_id: int) -> list[Punishment]:
    """Возвращает список активных наказаний пользователя."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Punishment).where(
                Punishment.user_id == user_id,
                Punishment.guild_id == guild_id,
                Punishment.active == True,
            ).order_by(Punishment.created_at.desc())
        )
        return list(result.scalars().all())


async def get_active_warn_count(user_id: int, guild_id: int) -> int:
    """Количество активных варнов."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.count()).where(
                Punishment.user_id == user_id,
                Punishment.guild_id == guild_id,
                Punishment.type == PunishmentType.warn,
                Punishment.active == True,
            )
        )
        return result.scalar_one()
