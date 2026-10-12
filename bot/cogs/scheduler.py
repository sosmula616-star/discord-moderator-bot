"""
Cog: Scheduler — авторазбан/авторазмут/истечение предупреждений.
Также обрабатывает on_member_join, on_member_update, on_guild_channel_create.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks
from sqlalchemy import select, and_

from bot.config_cache import config_cache
from bot.database import AsyncSessionLocal
from bot.models import (
    GuildSettings, Punishment, PunishmentType, User
)
from bot.utils.helpers import audit, send_log, safe_dm, utcnow, format_dt, punishment_embed
from bot.utils.punishments import (
    revoke_ban, revoke_mute, update_warn_roles, get_active_warn_count, upsert_user
)

log = logging.getLogger(__name__)


class SchedulerCog(commands.Cog, name="Scheduler"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._check_expired.start()
        self._send_root_heartbeat.start()

    def cog_unload(self):
        self._check_expired.cancel()
        self._send_root_heartbeat.cancel()

    # ─────────────────────────── Таймер истечения ───────────────────────────

    @tasks.loop(minutes=1)
    async def _check_expired(self):
        """Каждую минуту проверяет просроченные наказания."""
        now = utcnow()
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Punishment).where(
                    and_(
                        Punishment.active == True,
                        Punishment.expires_at != None,
                        Punishment.expires_at <= now,
                    )
                )
            )
            expired = result.scalars().all()

        for p in expired:
            guild = self.bot.get_guild(p.guild_id)
            if guild is None:
                continue

            cfg = await config_cache.get_or_load(p.guild_id)

            if p.type == PunishmentType.ban_role:
                try:
                    await revoke_ban(guild, p, self.bot.user.id, "Истёк срок бана", self.bot)
                    log.info(f"Scheduler: снят бан у {p.user_id}")
                    # Уведомление в лог
                    async with AsyncSessionLocal() as session:
                        s = await session.get(GuildSettings, p.guild_id)
                        ch_id = s.channel_log_punish_id if s else None
                    log_embed = punishment_embed(
                        title="🔓 Авторазбан",
                        color=discord.Color.green(),
                        reason="Истёк срок",
                        expires_at=p.expires_at,
                        auto=True,
                    )
                    log_embed.add_field(name="Пользователь", value=f"<@{p.user_id}>", inline=True)
                    await send_log(self.bot, ch_id, log_embed)
                    # DM
                    try:
                        user = await self.bot.fetch_user(p.user_id)
                        dm_embed = discord.Embed(
                            title=f"Бан на сервере {guild.name} истёк",
                            description="Вы можете снова зайти на сервер.",
                            color=discord.Color.green(),
                        )
                        await safe_dm(user, dm_embed)
                    except Exception:
                        pass
                except Exception as e:
                    log.error(f"Scheduler: ошибка авторазбана {p.id}: {e}")

            elif p.type in (PunishmentType.mute_voice, PunishmentType.mute_text):
                try:
                    await revoke_mute(guild, p, self.bot.user.id, "Истёк срок мута", cfg)
                    log.info(f"Scheduler: снят мут {p.type} у {p.user_id}")
                    async with AsyncSessionLocal() as session:
                        s = await session.get(GuildSettings, p.guild_id)
                        ch_id = s.channel_log_punish_id if s else None
                    log_embed = punishment_embed(
                        title=f"🔊 Авторазмут ({'голос' if p.type == PunishmentType.mute_voice else 'текст'})",
                        color=discord.Color.green(),
                        reason="Истёк срок",
                        auto=True,
                    )
                    log_embed.add_field(name="Пользователь", value=f"<@{p.user_id}>", inline=True)
                    await send_log(self.bot, ch_id, log_embed)
                except Exception as e:
                    log.error(f"Scheduler: ошибка авторазмута {p.id}: {e}")

            elif p.type in (PunishmentType.warn, PunishmentType.warning):
                try:
                    async with AsyncSessionLocal() as session:
                        _p = await session.get(Punishment, p.id)
                        if _p:
                            _p.active = False
                            await session.commit()

                    # Обновить варн-роли
                    member = guild.get_member(p.user_id)
                    if member and p.type == PunishmentType.warn:
                        wc = await get_active_warn_count(p.user_id, p.guild_id)
                        await update_warn_roles(guild, member, wc, cfg)
                    elif member and p.type == PunishmentType.warning:
                        warning_role = guild.get_role(cfg.role_warning_id) if cfg.role_warning_id else None
                        if warning_role and warning_role in member.roles:
                            try:
                                await member.remove_roles(warning_role, reason="Истёк срок предупреждения")
                            except (discord.Forbidden, discord.HTTPException):
                                pass
                except Exception as e:
                    log.error(f"Scheduler: ошибка истечения {p.type} {p.id}: {e}")

    @_check_expired.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────── Root Heartbeat ───────────────────────────

    @tasks.loop(seconds=30)
    async def _send_root_heartbeat(self):
        """Отправка периодического Heartbeat в консоль управления Root ovrdose."""
        root_url = os.environ.get("ROOT_PANEL_URL", "https://ovrdose.bothost.tech").rstrip("/")
        token = os.environ.get("INTERNAL_API_TOKEN", "root_internal_api_secret_ovrdose_token")
        ping_ms = round(self.bot.latency * 1000, 1) if self.bot.latency and self.bot.latency > 0 else 14.0
        uptime_sec = int((utcnow() - getattr(self.bot, "start_time", utcnow())).total_seconds())

        payload = {
            "bot": "warden",
            "pingMs": ping_ms,
            "ramMb": 64.0,
            "uptimeSec": max(uptime_sec, 1),
            "guildCount": len(self.bot.guilds),
        }
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{root_url}/api/internal/heartbeat",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=4),
                ) as resp:
                    pass
        except Exception:
            pass

    @_send_root_heartbeat.before_loop
    async def before_heartbeat(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────── on_member_join ───────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        await upsert_user(member)

        cfg = await config_cache.get_or_load(guild.id)
        now = utcnow()

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Punishment).where(
                    Punishment.user_id == member.id,
                    Punishment.guild_id == guild.id,
                    Punishment.active == True,
                )
            )
            active_punishments = result.scalars().all()

        # Проверяем, есть ли активный бан
        active_ban = next(
            (p for p in active_punishments if p.type == PunishmentType.ban_role), None
        )

        if active_ban:
            # Выдаём ТОЛЬКО роль бана
            if active_ban.expires_at and active_ban.expires_at <= now:
                # Бан истёк пока не было на сервере
                async with AsyncSessionLocal() as session:
                    p = await session.get(Punishment, active_ban.id)
                    if p:
                        p.active = False
                        await session.commit()
            else:
                ban_role = guild.get_role(cfg.role_ban_id) if cfg.role_ban_id else None
                if ban_role:
                    try:
                        await member.add_roles(ban_role, reason="Восстановление бана после входа")
                    except (discord.Forbidden, discord.HTTPException) as e:
                        log.error(f"on_member_join: не удалось выдать бан роль: {e}")
            return  # При активном бане остальное не применяем

        # Применяем остальные наказания
        roles_to_add = []
        for p in active_punishments:
            if p.expires_at and p.expires_at <= now:
                # Истекло пока не было
                async with AsyncSessionLocal() as session:
                    _p = await session.get(Punishment, p.id)
                    if _p:
                        _p.active = False
                        await session.commit()
                continue

            if p.type == PunishmentType.mute_voice:
                r = guild.get_role(cfg.role_mute_voice_id) if cfg.role_mute_voice_id else None
                if r:
                    roles_to_add.append(r)
            elif p.type == PunishmentType.mute_text:
                r = guild.get_role(cfg.role_mute_text_id) if cfg.role_mute_text_id else None
                if r:
                    roles_to_add.append(r)
            elif p.type == PunishmentType.warning:
                r = guild.get_role(cfg.role_warning_id) if cfg.role_warning_id else None
                if r:
                    roles_to_add.append(r)

        # Варн-роли по количеству
        warn_count = await get_active_warn_count(member.id, guild.id)
        if warn_count >= 2:
            r = guild.get_role(cfg.role_warn2_id) if cfg.role_warn2_id else None
            if r:
                roles_to_add.append(r)
        elif warn_count == 1:
            r = guild.get_role(cfg.role_warn1_id) if cfg.role_warn1_id else None
            if r:
                roles_to_add.append(r)

        # Допуск / недопуск / пол из БД
        async with AsyncSessionLocal() as session:
            db_user = await session.get(User, member.id)

        if db_user:
            if db_user.access_status == "access":
                r = guild.get_role(cfg.role_access_id) if cfg.role_access_id else None
                if r:
                    roles_to_add.append(r)
            elif db_user.access_status == "noaccess":
                r = guild.get_role(cfg.role_noaccess_id) if cfg.role_noaccess_id else None
                if r:
                    roles_to_add.append(r)
            if db_user.gender == "male":
                r = guild.get_role(cfg.role_male_id) if cfg.role_male_id else None
                if r:
                    roles_to_add.append(r)
            elif db_user.gender == "female":
                r = guild.get_role(cfg.role_female_id) if cfg.role_female_id else None
                if r:
                    roles_to_add.append(r)

        if roles_to_add:
            unique_roles = list({r.id: r for r in roles_to_add}.values())
            try:
                await member.add_roles(*unique_roles, reason="Восстановление ролей после входа")
            except (discord.Forbidden, discord.HTTPException) as e:
                log.error(f"on_member_join: не удалось восстановить роли: {e}")

    # ─────────────────────────── on_member_update (защита от обхода) ───────────────────────────

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild
        cfg = await config_cache.get_or_load(guild.id)
        ban_role_id = cfg.role_ban_id
        if not ban_role_id:
            return

        ban_role = guild.get_role(ban_role_id)
        if not ban_role:
            return

        before_had_ban = ban_role in before.roles
        after_has_ban = ban_role in after.roles

        # Бан-роль была снята
        if before_had_ban and not after_has_ban:
            # Проверяем активный бан в БД
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Punishment).where(
                        Punishment.user_id == after.id,
                        Punishment.guild_id == guild.id,
                        Punishment.type == PunishmentType.ban_role,
                        Punishment.active == True,
                    )
                )
                active_ban = result.scalar_one_or_none()

            if active_ban:
                # Возвращаем бан-роль немедленно
                try:
                    await after.add_roles(ban_role, reason="Защита от обхода бана")
                    # Убираем все роли, которые не должны быть у забаненного
                    current_roles = [r for r in after.roles if r != guild.default_role and r != ban_role and not r.managed]
                    if current_roles:
                        await after.remove_roles(*current_roles, reason="Защита от обхода бана")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.error(f"on_member_update: не удалось восстановить бан: {e}")

                # Определяем кто снял (через audit log)
                removed_by = "неизвестно"
                try:
                    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update):
                        if entry.target.id == after.id:
                            removed_by = f"{entry.user} ({entry.user.id})"
                            break
                except (discord.Forbidden, discord.HTTPException):
                    pass

                log.warning(f"Попытка обхода бана: пользователь {after.id}, снял {removed_by}")

                # Лог в канал
                async with AsyncSessionLocal() as session:
                    s = await session.get(GuildSettings, guild.id)
                    ch_id = s.channel_log_punish_id if s else None

                log_embed = discord.Embed(
                    title="⚠️ Попытка обхода бана",
                    color=discord.Color.red(),
                    timestamp=utcnow(),
                )
                log_embed.add_field(name="Пользователь", value=f"{after.mention} (`{after.id}`)", inline=True)
                log_embed.add_field(name="Снял роль", value=removed_by, inline=True)
                await send_log(self.bot, ch_id, log_embed)

                await audit(guild.id, "ban_bypass_attempt", target_id=after.id,
                            details=f"Роль бана снята пользователем: {removed_by}")
                return

        # Если пользователь под баном — убираем добавленные роли
        if after_has_ban:
            added_roles = set(after.roles) - set(before.roles)
            added_non_ban = [r for r in added_roles if r != ban_role and not r.managed]
            if added_non_ban:
                try:
                    await after.remove_roles(*added_non_ban, reason="Запрет выдачи ролей забаненному")
                except (discord.Forbidden, discord.HTTPException):
                    pass

    # ─────────────────────────── on_member_remove ───────────────────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        async with AsyncSessionLocal() as session:
            db_user = await session.get(User, member.id)
            if db_user:
                db_user.left_at = utcnow()
                await session.commit()

    # ─────────────────────────── on_guild_channel_create ───────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        """Применяет overrides бан-роли к новому каналу."""
        guild = channel.guild
        cfg = await config_cache.get_or_load(guild.id)

        if not cfg.role_ban_id:
            return

        ban_role = guild.get_role(cfg.role_ban_id)
        if not ban_role:
            return

        # Определяем тип канала
        is_ban_room = channel.id == cfg.channel_ban_room_id
        overwrite = (
            discord.PermissionOverwrite(view_channel=True, read_message_history=True)
            if is_ban_room
            else discord.PermissionOverwrite(
                view_channel=False, connect=False, send_messages=False,
                speak=False, read_message_history=False,
            )
        )
        try:
            await channel.set_permissions(ban_role, overwrite=overwrite, reason="Авто-синхронизация прав бана")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"on_guild_channel_create: не удалось выставить права на {channel.name}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulerCog(bot))
