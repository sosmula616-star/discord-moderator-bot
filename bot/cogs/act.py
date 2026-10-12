"""
Cog: /act — главное меню модерации.
Содержит slash-команду, динамическую View с кнопками, модалы и логику действий.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, func

from bot.config_cache import config_cache
from bot.database import AsyncSessionLocal
from bot.models import Punishment, PunishmentType, User, GuildSettings
from bot.utils.helpers import (
    audit, send_log, punishment_embed, safe_dm, utcnow,
    format_dt, format_duration
)
from bot.utils.punishments import (
    apply_ban, revoke_ban, apply_mute, revoke_mute,
    apply_warn, apply_warning, update_warn_roles,
    get_active_punishments, get_active_warn_count,
    upsert_user,
)

log = logging.getLogger(__name__)

# ─────────────────────────── Проверки иерархии ───────────────────────────

def check_hierarchy(
    moderator: discord.Member,
    target: discord.Member | discord.User,
    guild: discord.Guild,
) -> Optional[str]:
    """Возвращает строку с ошибкой или None, если всё ок."""
    if isinstance(target, discord.User):
        return None  # пользователь не на сервере — проверить нельзя
    if target.id == moderator.id:
        return "❌ Нельзя применять действие к самому себе."
    if target.bot:
        return "❌ Нельзя применять действие к боту."
    if target.id == guild.owner_id:
        return "❌ Нельзя применять действие к владельцу сервера."
    if target.top_role >= moderator.top_role:
        return "❌ Нельзя применять действие к пользователю с равной или высшей ролью."
    return None


def has_action_permission(moderator: discord.Member, action_key: str, cfg) -> bool:
    """Проверяет, разрешено ли модератору видеть эту кнопку (по настройкам из панели)."""
    btn = cfg.buttons.get(action_key)
    if not btn or not btn["enabled"]:
        return False
    allowed_roles = btn["role_ids"]
    if not allowed_roles:
        return True  # кнопка без ограничений видна всем модераторам
    mod_role_ids = {r.id for r in moderator.roles}
    return bool(mod_role_ids & set(allowed_roles))


# ─────────────────────────── Build user embed ───────────────────────────

async def build_user_embed(
    user_id: int,
    guild: discord.Guild,
    bot: discord.Client,
) -> discord.Embed:
    """Строит embed с карточкой пользователя."""
    member: Optional[discord.Member] = guild.get_member(user_id)
    discord_user: Optional[discord.User] = bot.get_user(user_id)

    if discord_user is None:
        try:
            discord_user = await bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            pass

    # Данные из БД
    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user_id)

    active_punishments = await get_active_punishments(user_id, guild.id)
    warn_count = await get_active_warn_count(user_id, guild.id)

    embed = discord.Embed(
        title="Карточка пользователя",
        color=discord.Color.blurple(),
        timestamp=utcnow(),
    )

    if discord_user:
        embed.set_thumbnail(url=discord_user.display_avatar.url)
        embed.add_field(name="👤 Пользователь", value=f"{discord_user} (`{discord_user.id}`)", inline=False)
        embed.add_field(name="📅 Аккаунт создан", value=format_dt(discord_user.created_at), inline=True)
    else:
        embed.add_field(name="👤 ID", value=str(user_id), inline=False)

    if member:
        embed.add_field(name="📥 Зашёл на сервер", value=format_dt(member.joined_at), inline=True)
        embed.add_field(name="🎭 Ник", value=member.display_name, inline=True)
    elif db_user and db_user.joined_at:
        embed.add_field(name="📥 Последний заход", value=format_dt(db_user.joined_at), inline=True)
        embed.add_field(name="🚪 Статус", value="Не на сервере", inline=True)

    # Пол и допуск
    gender_str = "—"
    access_str = "—"
    if db_user:
        if db_user.gender == "male":
            gender_str = "♂ Мужской"
        elif db_user.gender == "female":
            gender_str = "♀ Женский"
        if db_user.access_status == "access":
            access_str = "✅ Допущен"
        elif db_user.access_status == "noaccess":
            access_str = "🚫 Недопуск"

    embed.add_field(name="⚧ Пол", value=gender_str, inline=True)
    embed.add_field(name="🔑 Допуск", value=access_str, inline=True)

    # Варны
    embed.add_field(name="⚠️ Варны", value=f"{warn_count}/3", inline=True)

    # Активные наказания
    if active_punishments:
        punish_lines = []
        for p in active_punishments:
            type_labels = {
                PunishmentType.ban_role:   "🔨 Бан (роль)",
                PunishmentType.mute_voice: "🔇 Мут голос",
                PunishmentType.mute_text:  "✏️ Мут текст",
                PunishmentType.warn:       "⚠️ Варн",
                PunishmentType.warning:    "🔔 Предупреждение",
            }
            label = type_labels.get(p.type, str(p.type))
            expires = f"до {format_dt(p.expires_at)}" if p.expires_at else "навсегда"
            punish_lines.append(f"• {label} — {expires} | причина: {p.reason[:60]}")
        embed.add_field(
            name="🚨 Активные наказания",
            value="\n".join(punish_lines[:5]),
            inline=False
        )
    else:
        embed.add_field(name="✅ Наказаний нет", value="Пользователь чист", inline=False)

    embed.set_footer(text=f"Сервер: {guild.name}")
    return embed


# ─────────────────────────── Модалы ───────────────────────────

class BanReasonModal(discord.ui.Modal, title="Бан: причина и описание"):
    reason = discord.ui.TextInput(
        label="Причина", placeholder="Краткая причина бана", max_length=512, required=True
    )
    description = discord.ui.TextInput(
        label="Описание ситуации",
        style=discord.TextStyle.paragraph,
        placeholder="Подробное описание",
        max_length=2000,
        required=True,
    )

    def __init__(self, parent_view: "ActView", duration_days: Optional[int]):
        super().__init__()
        self.parent_view = parent_view
        self.duration_days = duration_days  # None = навсегда

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        expires_at = None
        if self.duration_days is not None:
            expires_at = utcnow() + timedelta(days=self.duration_days)

        guild = interaction.guild
        moderator = interaction.user
        target_id = self.parent_view.target_id

        member = guild.get_member(target_id)
        if member is None:
            await interaction.followup.send("❌ Пользователь не найден на сервере.", ephemeral=True)
            return

        err = check_hierarchy(moderator, member, guild)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        cfg = await config_cache.get_or_load(guild.id)

        try:
            punishment = await apply_ban(
                guild=guild,
                target=member,
                moderator_id=moderator.id,
                reason=self.reason.value,
                description=self.description.value,
                expires_at=expires_at,
                cfg=cfg,
            )
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        # DM пользователю
        duration_str = f"{self.duration_days} дн." if self.duration_days else "навсегда"
        dm_embed = discord.Embed(
            title=f"Вы получили бан на сервере {guild.name}",
            description=f"**Причина:** {self.reason.value}\n**Срок:** {duration_str}",
            color=discord.Color.red(),
        )
        await safe_dm(member, dm_embed)

        # Лог
        log_embed = punishment_embed(
            title="🔨 Бан (роль) выдан",
            color=discord.Color.dark_red(),
            moderator=moderator,
            target=member,
            reason=self.reason.value,
            description=self.description.value,
            duration=duration_str,
            expires_at=expires_at,
        )

        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            ch_id = s.channel_log_punish_id if s else None
        await send_log(interaction.client, ch_id, log_embed)

        # Обновляем embed
        new_embed = await build_user_embed(target_id, guild, interaction.client)
        try:
            await interaction.edit_original_response(embed=new_embed, view=self.parent_view)
        except Exception:
            pass
        await interaction.followup.send("✅ Бан выдан.", ephemeral=True)


class CustomDaysModal(discord.ui.Modal, title="Свой срок бана"):
    days = discord.ui.TextInput(
        label="Количество дней (1–90)",
        placeholder="Введите число от 1 до 90",
        max_length=3,
        required=True,
    )

    def __init__(self, parent_view: "ActView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            d = int(self.days.value)
            if not 1 <= d <= 90:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Введите целое число от 1 до 90.", ephemeral=True
            )
            return
        modal = BanReasonModal(self.parent_view, d)
        await interaction.response.send_modal(modal)


class MuteReasonModal(discord.ui.Modal, title="Мут: причина и описание"):
    reason = discord.ui.TextInput(
        label="Причина", max_length=512, required=True
    )
    description = discord.ui.TextInput(
        label="Описание",
        style=discord.TextStyle.paragraph,
        max_length=2000,
        required=True,
    )

    def __init__(self, parent_view: "ActView", mute_type: str, duration_minutes: int):
        super().__init__()
        self.parent_view = parent_view
        self.mute_type = mute_type
        self.duration_minutes = duration_minutes

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        moderator = interaction.user
        target_id = self.parent_view.target_id
        member = guild.get_member(target_id)

        if member is None:
            await interaction.followup.send("❌ Пользователь не на сервере.", ephemeral=True)
            return

        err = check_hierarchy(moderator, member, guild)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        cfg = await config_cache.get_or_load(guild.id)
        if self.duration_minutes > cfg.max_mute_minutes:
            await interaction.followup.send(
                f"❌ Максимальный срок мута: {cfg.max_mute_minutes} мин.", ephemeral=True
            )
            return

        expires_at = utcnow() + timedelta(minutes=self.duration_minutes)
        try:
            punishment = await apply_mute(
                guild=guild,
                target=member,
                moderator_id=moderator.id,
                mute_type=self.mute_type,
                reason=self.reason.value,
                description=self.description.value,
                expires_at=expires_at,
                cfg=cfg,
            )
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        type_label = "голосовой" if self.mute_type == "voice" else "текстовый"
        duration_str = format_duration(self.duration_minutes * 60)

        log_embed = punishment_embed(
            title=f"🔇 Мут ({type_label}) выдан",
            color=discord.Color.orange(),
            moderator=moderator,
            target=member,
            reason=self.reason.value,
            description=self.description.value,
            duration=duration_str,
            expires_at=expires_at,
        )
        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            ch_id = s.channel_log_punish_id if s else None
        await send_log(interaction.client, ch_id, log_embed)

        new_embed = await build_user_embed(target_id, guild, interaction.client)
        try:
            await interaction.edit_original_response(embed=new_embed, view=self.parent_view)
        except Exception:
            pass
        await interaction.followup.send(f"✅ Мут ({type_label}) на {duration_str} выдан.", ephemeral=True)


class CustomMuteDurationModal(discord.ui.Modal, title="Свой срок мута"):
    minutes = discord.ui.TextInput(
        label="Количество минут",
        placeholder="Введите число минут",
        max_length=6,
        required=True,
    )

    def __init__(self, parent_view: "ActView", mute_type: str, max_minutes: int):
        super().__init__()
        self.parent_view = parent_view
        self.mute_type = mute_type
        self.max_minutes = max_minutes

    async def on_submit(self, interaction: discord.Interaction):
        try:
            m = int(self.minutes.value)
            if not 1 <= m <= self.max_minutes:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                f"❌ Введите целое число от 1 до {self.max_minutes}.", ephemeral=True
            )
            return
        modal = MuteReasonModal(self.parent_view, self.mute_type, m)
        await interaction.response.send_modal(modal)


class WarnModal(discord.ui.Modal, title="Варн: причина и описание"):
    reason = discord.ui.TextInput(label="Причина", max_length=512, required=True)
    description = discord.ui.TextInput(
        label="Описание",
        style=discord.TextStyle.paragraph,
        max_length=2000,
        required=True,
    )

    def __init__(self, parent_view: "ActView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        moderator = interaction.user
        target_id = self.parent_view.target_id
        member = guild.get_member(target_id)

        if member is None:
            await interaction.followup.send("❌ Пользователь не на сервере.", ephemeral=True)
            return

        err = check_hierarchy(moderator, member, guild)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        cfg = await config_cache.get_or_load(guild.id)
        punishment = await apply_warn(
            guild=guild, target=member, moderator_id=moderator.id,
            reason=self.reason.value, description=self.description.value,
            cfg=cfg, bot=interaction.client,
        )

        log_embed = punishment_embed(
            title="⚠️ Варн выдан",
            color=discord.Color.orange(),
            moderator=moderator,
            target=member,
            reason=self.reason.value,
            description=self.description.value,
        )
        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            ch_id = s.channel_log_punish_id if s else None
        await send_log(interaction.client, ch_id, log_embed)

        new_embed = await build_user_embed(target_id, guild, interaction.client)
        try:
            await interaction.edit_original_response(embed=new_embed, view=self.parent_view)
        except Exception:
            pass
        await interaction.followup.send("✅ Варн выдан.", ephemeral=True)


class WarningReasonModal(discord.ui.Modal, title="Предупреждение: причина"):
    reason = discord.ui.TextInput(label="Причина", max_length=512, required=True)

    def __init__(self, parent_view: "ActView", duration_hours: int):
        super().__init__()
        self.parent_view = parent_view
        self.duration_hours = duration_hours

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        moderator = interaction.user
        target_id = self.parent_view.target_id
        member = guild.get_member(target_id)

        if member is None:
            await interaction.followup.send("❌ Пользователь не на сервере.", ephemeral=True)
            return

        err = check_hierarchy(moderator, member, guild)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        cfg = await config_cache.get_or_load(guild.id)
        await apply_warning(
            guild=guild, target=member, moderator_id=moderator.id,
            reason=self.reason.value, duration_hours=self.duration_hours, cfg=cfg,
        )

        log_embed = punishment_embed(
            title="🔔 Предупреждение выдано",
            color=discord.Color.yellow(),
            moderator=moderator,
            target=member,
            reason=self.reason.value,
            duration=f"{self.duration_hours} ч.",
        )
        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            ch_id = s.channel_log_punish_id if s else None
        await send_log(interaction.client, ch_id, log_embed)

        new_embed = await build_user_embed(target_id, guild, interaction.client)
        try:
            await interaction.edit_original_response(embed=new_embed, view=self.parent_view)
        except Exception:
            pass
        await interaction.followup.send(
            f"✅ Предупреждение на {self.duration_hours} ч. выдано.", ephemeral=True
        )


class RevokeReasonModal(discord.ui.Modal, title="Причина снятия наказания"):
    reason = discord.ui.TextInput(label="Причина", max_length=512, required=True)

    def __init__(self, parent_view: "ActView", selected_ids: list[int]):
        super().__init__()
        self.parent_view = parent_view
        self.selected_ids = selected_ids

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        moderator = interaction.user
        target_id = self.parent_view.target_id
        cfg = await config_cache.get_or_load(guild.id)

        async with AsyncSessionLocal() as session:
            for p_id in self.selected_ids:
                p = await session.get(Punishment, p_id)
                if not p or not p.active or p.guild_id != guild.id or p.user_id != target_id:
                    continue
                if p.type == PunishmentType.ban_role:
                    await revoke_ban(guild, p, moderator.id, self.reason.value, interaction.client)
                elif p.type in (PunishmentType.mute_voice, PunishmentType.mute_text):
                    await revoke_mute(guild, p, moderator.id, self.reason.value, cfg)
                else:
                    # warn / warning — просто снимаем активность
                    p.active = False
                    p.revoked_by = moderator.id
                    p.revoked_at = utcnow()
                    p.revoke_reason = self.reason.value
                    await session.commit()
                    # Обновляем варн-роли
                    member = guild.get_member(target_id)
                    if member and p.type == PunishmentType.warn:
                        wc = await get_active_warn_count(target_id, guild.id)
                        await update_warn_roles(guild, member, wc, cfg)
                    elif member and p.type == PunishmentType.warning:
                        wr = guild.get_role(cfg.role_warning_id) if cfg.role_warning_id else None
                        if wr and wr in member.roles:
                            try:
                                await member.remove_roles(wr, reason=f"Снятие предупреждения: {self.reason.value}")
                            except discord.Forbidden:
                                pass

        await audit(guild.id, "punishments_revoke", actor_id=moderator.id,
                    target_id=target_id, details=self.reason.value)

        new_embed = await build_user_embed(target_id, guild, interaction.client)
        try:
            await interaction.edit_original_response(embed=new_embed, view=self.parent_view)
        except Exception:
            pass
        await interaction.followup.send("✅ Наказания сняты.", ephemeral=True)


# ─────────────────────────── Select для снятия ───────────────────────────

class RevokeSelect(discord.ui.Select):
    def __init__(self, punishments: list[Punishment], parent_view: "ActView"):
        self.parent_view = parent_view
        type_labels = {
            PunishmentType.ban_role:   "Бан (роль)",
            PunishmentType.mute_voice: "Мут голос",
            PunishmentType.mute_text:  "Мут текст",
            PunishmentType.warn:       "Варн",
            PunishmentType.warning:    "Предупреждение",
        }
        options = [
            discord.SelectOption(
                label=f"{type_labels.get(p.type, p.type)} #{p.id}",
                description=p.reason[:50],
                value=str(p.id),
            )
            for p in punishments[:25]
        ]
        super().__init__(
            placeholder="Выберите наказания для снятия...",
            min_values=1,
            max_values=min(len(options), 25),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_ids = [int(v) for v in self.values]
        await interaction.response.send_modal(
            RevokeReasonModal(self.parent_view, selected_ids)
        )


class RevokeView(discord.ui.View):
    def __init__(self, punishments: list[Punishment], parent_view: "ActView"):
        super().__init__(timeout=120)
        self.add_item(RevokeSelect(punishments, parent_view))


# ─────────────────────────── Select для сроков мута ───────────────────────────

class MuteDurationView(discord.ui.View):
    def __init__(self, parent_view: "ActView", mute_type: str, max_minutes: int):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self.mute_type = mute_type
        self.max_minutes = max_minutes
        self._add_buttons()

    def _add_buttons(self):
        durations = [
            ("15 мин.", 15), ("30 мин.", 30), ("60 мин.", 60), ("120 мин.", 120)
        ]
        for label, minutes in durations:
            if minutes <= self.max_minutes:
                btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
                btn.callback = self._make_callback(minutes)
                self.add_item(btn)

        custom_btn = discord.ui.Button(label="Свой срок", style=discord.ButtonStyle.primary)
        custom_btn.callback = self._custom_callback
        self.add_item(custom_btn)

    def _make_callback(self, minutes: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.send_modal(
                MuteReasonModal(self.parent_view, self.mute_type, minutes)
            )
        return callback

    async def _custom_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            CustomMuteDurationModal(self.parent_view, self.mute_type, self.max_minutes)
        )


# ─────────────────────────── Главная View /act ───────────────────────────

class ActView(discord.ui.View):
    """Persistent view для меню /act. custom_id = act:{guild_id}:{target_id}"""

    def __init__(self, target_id: int, moderator_id: int, guild_id: int, cfg):
        super().__init__(timeout=300)
        self.target_id = target_id
        self.moderator_id = moderator_id
        self.guild_id = guild_id
        self.cfg = cfg
        self._build_buttons()

    def _get_style(self, action_key: str) -> discord.ButtonStyle:
        btn = self.cfg.buttons.get(action_key, {})
        style_map = {
            "primary":   discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success":   discord.ButtonStyle.success,
            "danger":    discord.ButtonStyle.danger,
        }
        return style_map.get(btn.get("style", "secondary"), discord.ButtonStyle.secondary)

    def _btn_label(self, action_key: str, default: str) -> str:
        btn = self.cfg.buttons.get(action_key, {})
        label = btn.get("label", default)
        emoji = btn.get("emoji")
        return f"{emoji} {label}" if emoji else label

    def _build_buttons(self):
        """Динамически строит кнопки по конфигу."""
        # Сортируем по order
        sorted_buttons = sorted(
            self.cfg.buttons.items(),
            key=lambda x: x[1].get("order", 99),
        )

        for action_key, btn_cfg in sorted_buttons:
            if not btn_cfg.get("enabled", True):
                continue

            label = btn_cfg.get("label", action_key)
            emoji = btn_cfg.get("emoji")
            style_str = btn_cfg.get("style", "secondary")
            style_map = {
                "primary":   discord.ButtonStyle.primary,
                "secondary": discord.ButtonStyle.secondary,
                "success":   discord.ButtonStyle.success,
                "danger":    discord.ButtonStyle.danger,
            }
            style = style_map.get(style_str, discord.ButtonStyle.secondary)

            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                custom_id=f"act_{action_key}_{self.guild_id}_{self.target_id}",
            )
            button.callback = self._make_action_callback(action_key)
            self.add_item(button)

    def _make_action_callback(self, action_key: str):
        async def callback(interaction: discord.Interaction):
            await self._handle_action(interaction, action_key)
        return callback

    async def _check_permission(self, interaction: discord.Interaction, action_key: str) -> bool:
        """Перепроверяет права на каждое нажатие."""
        cfg = await config_cache.get_or_load(interaction.guild_id)
        moderator = interaction.user
        if not has_action_permission(moderator, action_key, cfg):
            await interaction.response.send_message(
                "❌ У вас нет прав для этого действия.", ephemeral=True
            )
            return False
        return True

    async def _handle_action(self, interaction: discord.Interaction, action_key: str):
        if not await self._check_permission(interaction, action_key):
            return

        guild = interaction.guild
        moderator = interaction.user
        target_id = self.target_id
        cfg = await config_cache.get_or_load(guild.id)

        # ── Верификация ──────────────────────────────────────────────
        if action_key == "verify_access":
            await self._verify_action(interaction, "access", cfg)

        elif action_key == "verify_noaccess":
            await self._verify_action(interaction, "noaccess", cfg)

        elif action_key == "verify_male":
            await self._gender_action(interaction, "male", cfg)

        elif action_key == "verify_female":
            await self._gender_action(interaction, "female", cfg)

        # ── Бан ─────────────────────────────────────────────────────
        elif action_key == "ban":
            member = guild.get_member(target_id)
            if member is None:
                await interaction.response.send_message("❌ Пользователь не на сервере.", ephemeral=True)
                return
            err = check_hierarchy(moderator, member, guild)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return
            await interaction.response.send_message(
                "Выберите срок бана:", view=BanDurationView(self), ephemeral=True
            )

        # ── Муты ────────────────────────────────────────────────────
        elif action_key == "mute_voice":
            member = guild.get_member(target_id)
            if member is None:
                await interaction.response.send_message("❌ Пользователь не на сервере.", ephemeral=True)
                return
            err = check_hierarchy(moderator, member, guild)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return
            await interaction.response.send_message(
                "Выберите срок мута:", view=MuteDurationView(self, "voice", cfg.max_mute_minutes),
                ephemeral=True
            )

        elif action_key == "mute_text":
            member = guild.get_member(target_id)
            if member is None:
                await interaction.response.send_message("❌ Пользователь не на сервере.", ephemeral=True)
                return
            err = check_hierarchy(moderator, member, guild)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return
            await interaction.response.send_message(
                "Выберите срок мута:", view=MuteDurationView(self, "text", cfg.max_mute_minutes),
                ephemeral=True
            )

        # ── Варн ────────────────────────────────────────────────────
        elif action_key == "warn":
            member = guild.get_member(target_id)
            if member is None:
                await interaction.response.send_message("❌ Пользователь не на сервере.", ephemeral=True)
                return
            err = check_hierarchy(moderator, member, guild)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return
            await interaction.response.send_modal(WarnModal(self))

        # ── Предупреждение ──────────────────────────────────────────
        elif action_key == "warning":
            member = guild.get_member(target_id)
            if member is None:
                await interaction.response.send_message("❌ Пользователь не на сервере.", ephemeral=True)
                return
            err = check_hierarchy(moderator, member, guild)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return
            await interaction.response.send_message(
                "Выберите срок предупреждения:", view=WarningDurationView(self), ephemeral=True
            )

        # ── Жалобы на пользователя ──────────────────────────────────
        elif action_key == "reports_view":
            await self._show_reports(interaction)

        # ── История наказаний ────────────────────────────────────────
        elif action_key == "history":
            await self._show_history(interaction)

        # ── Снятие наказаний ─────────────────────────────────────────
        elif action_key == "revoke":
            active = await get_active_punishments(target_id, guild.id)
            if not active:
                await interaction.response.send_message(
                    "❌ У пользователя нет активных наказаний.", ephemeral=True
                )
                return
            await interaction.response.send_message(
                "Выберите наказания для снятия:",
                view=RevokeView(active, self),
                ephemeral=True,
            )

    async def _verify_action(self, interaction: discord.Interaction, action_type: str, cfg):
        """Выдаёт/снимает роли допуска/недопуска."""
        guild = interaction.guild
        target_id = self.target_id
        member = guild.get_member(target_id)
        if member is None:
            await interaction.response.send_message("❌ Пользователь не на сервере.", ephemeral=True)
            return

        access_role = guild.get_role(cfg.role_access_id) if cfg.role_access_id else None
        noaccess_role = guild.get_role(cfg.role_noaccess_id) if cfg.role_noaccess_id else None

        await interaction.response.defer(ephemeral=True)
        try:
            if action_type == "access":
                if access_role:
                    await member.add_roles(access_role, reason="Верификация: допуск")
                if noaccess_role and noaccess_role in member.roles:
                    await member.remove_roles(noaccess_role, reason="Верификация: допуск")
                label = "Допуск выдан"
                db_status = "access"
            else:
                if noaccess_role:
                    await member.add_roles(noaccess_role, reason="Верификация: недопуск")
                if access_role and access_role in member.roles:
                    await member.remove_roles(access_role, reason="Верификация: недопуск")
                label = "Недопуск выдан"
                db_status = "noaccess"
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав для управления ролями.", ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            u = await session.get(User, target_id)
            if u:
                u.access_status = db_status
            await session.commit()

        await audit(guild.id, f"verify_{action_type}", actor_id=interaction.user.id,
                    target_id=target_id, details=label)

        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            ch_id = s.channel_log_verify_id if s else None

        log_embed = discord.Embed(
            title=f"🔑 Верификация: {label}",
            color=discord.Color.green() if action_type == "access" else discord.Color.red(),
            timestamp=utcnow(),
        )
        log_embed.add_field(name="👮 Модератор", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="🎯 Цель", value=f"{member.mention} (`{member.id}`)", inline=True)
        await send_log(interaction.client, ch_id, log_embed)

        new_embed = await build_user_embed(target_id, guild, interaction.client)
        try:
            await interaction.edit_original_response(embed=new_embed, view=self)
        except Exception:
            pass
        await interaction.followup.send(f"✅ {label}.", ephemeral=True)

    async def _gender_action(self, interaction: discord.Interaction, gender: str, cfg):
        """Выдаёт/снимает роли пола."""
        guild = interaction.guild
        member = guild.get_member(self.target_id)
        if member is None:
            await interaction.response.send_message("❌ Пользователь не на сервере.", ephemeral=True)
            return

        male_role = guild.get_role(cfg.role_male_id) if cfg.role_male_id else None
        female_role = guild.get_role(cfg.role_female_id) if cfg.role_female_id else None

        await interaction.response.defer(ephemeral=True)
        try:
            if gender == "male":
                if male_role:
                    await member.add_roles(male_role, reason="Верификация: пол мужской")
                if female_role and female_role in member.roles:
                    await member.remove_roles(female_role)
                label = "Мужской пол выдан"
            else:
                if female_role:
                    await member.add_roles(female_role, reason="Верификация: пол женский")
                if male_role and male_role in member.roles:
                    await member.remove_roles(male_role)
                label = "Женский пол выдан"
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав для управления ролями.", ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            u = await session.get(User, self.target_id)
            if u:
                u.gender = gender
            await session.commit()

        await audit(guild.id, f"gender_{gender}", actor_id=interaction.user.id, target_id=self.target_id)

        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            ch_id = s.channel_log_verify_id if s else None
        log_embed = discord.Embed(
            title=f"⚧ Пол: {label}",
            color=discord.Color.blue() if gender == "male" else discord.Color(0xFFB6C1),
            timestamp=utcnow(),
        )
        log_embed.add_field(name="👮 Модератор", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="🎯 Цель", value=f"{member.mention} (`{member.id}`)", inline=True)
        await send_log(interaction.client, ch_id, log_embed)

        new_embed = await build_user_embed(self.target_id, guild, interaction.client)
        try:
            await interaction.edit_original_response(embed=new_embed, view=self)
        except Exception:
            pass
        await interaction.followup.send(f"✅ {label}.", ephemeral=True)

    async def _show_reports(self, interaction: discord.Interaction):
        from bot.models import Report, ReportStatus
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Report).where(
                    Report.defendant_id == self.target_id,
                    Report.guild_id == interaction.guild_id,
                ).order_by(Report.created_at.desc()).limit(10)
            )
            reports = result.scalars().all()

        if not reports:
            await interaction.response.send_message("📋 Жалоб на этого пользователя нет.", ephemeral=True)
            return

        embed = discord.Embed(title="📋 Жалобы на пользователя", color=discord.Color.blurple(), timestamp=utcnow())
        status_labels = {
            "open": "Открыта", "accepted": "Принята", "rejected": "Отклонена", "closed": "Закрыта"
        }
        for r in reports:
            embed.add_field(
                name=f"#{r.id} | {status_labels.get(r.status.value, r.status.value)}",
                value=f"Автор: <@{r.plaintiff_id}> | {r.text[:80]}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _show_history(self, interaction: discord.Interaction):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Punishment).where(
                    Punishment.user_id == self.target_id,
                    Punishment.guild_id == interaction.guild_id,
                ).order_by(Punishment.created_at.desc()).limit(15)
            )
            punishments = result.scalars().all()

        if not punishments:
            await interaction.response.send_message("📜 История наказаний пуста.", ephemeral=True)
            return

        embed = discord.Embed(title="📜 История наказаний", color=discord.Color.blurple(), timestamp=utcnow())
        type_labels = {
            PunishmentType.ban_role:   "Бан (роль)",
            PunishmentType.mute_voice: "Мут голос",
            PunishmentType.mute_text:  "Мут текст",
            PunishmentType.warn:       "Варн",
            PunishmentType.warning:    "Предупреждение",
        }
        for p in punishments:
            status = "🟢 Активно" if p.active else "🔴 Снято/истекло"
            revoke_info = ""
            if not p.active and p.revoked_by:
                revoke_info = f"\nСнял: <@{p.revoked_by}> | {p.revoke_reason or '—'}"
            embed.add_field(
                name=f"#{p.id} {type_labels.get(p.type, str(p.type))} | {status}",
                value=(
                    f"Причина: {p.reason[:80]}\n"
                    f"Модератор: <@{p.moderator_id}>\n"
                    f"Дата: {format_dt(p.created_at)}"
                    f"{revoke_info}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────── Срок бана View ───────────────────────────

class BanDurationView(discord.ui.View):
    def __init__(self, parent_view: ActView):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self._add_buttons()

    def _add_buttons(self):
        options = [
            ("Навсегда", None), ("1 день", 1), ("5 дней", 5),
            ("10 дней", 10), ("15 дней", 15), ("30 дней", 30),
        ]
        for label, days in options:
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            btn.callback = self._make_callback(days)
            self.add_item(btn)
        custom = discord.ui.Button(label="Свой срок", style=discord.ButtonStyle.primary)
        custom.callback = self._custom_callback
        self.add_item(custom)

    def _make_callback(self, days: Optional[int]):
        async def callback(interaction: discord.Interaction):
            await interaction.response.send_modal(BanReasonModal(self.parent_view, days))
        return callback

    async def _custom_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CustomDaysModal(self.parent_view))


class WarningDurationView(discord.ui.View):
    def __init__(self, parent_view: ActView):
        super().__init__(timeout=60)
        self.parent_view = parent_view

    @discord.ui.button(label="1 час", style=discord.ButtonStyle.secondary)
    async def one_hour(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WarningReasonModal(self.parent_view, 1))

    @discord.ui.button(label="6 часов", style=discord.ButtonStyle.secondary)
    async def six_hours(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WarningReasonModal(self.parent_view, 6))


# ─────────────────────────── Cog ───────────────────────────

class ActCog(commands.Cog, name="Act"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="act", description="Меню модерации пользователя")
    @app_commands.describe(user="Пользователь или его ID")
    @app_commands.guild_only()
    async def act(self, interaction: discord.Interaction, user: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # Парсим user: может быть упоминание, ID или ник
        target_id: Optional[int] = None
        member: Optional[discord.Member] = None

        # Очищаем упоминание
        clean = user.strip().lstrip("<@!").rstrip(">")
        try:
            target_id = int(clean)
        except ValueError:
            await interaction.followup.send(
                "❌ Укажите ID пользователя или упоминание (@user).", ephemeral=True
            )
            return

        member = guild.get_member(target_id)
        if member is None:
            try:
                member = await guild.fetch_member(target_id)
            except (discord.NotFound, discord.HTTPException):
                pass

        cfg = await config_cache.get_or_load(guild.id)

        # Строим embed и view
        embed = await build_user_embed(target_id, guild, self.bot)
        view = ActView(
            target_id=target_id,
            moderator_id=interaction.user.id,
            guild_id=guild.id,
            cfg=cfg,
        )

        # Фильтруем кнопки по правам модератора
        moderator = interaction.user
        buttons_to_remove = []
        for item in view.children:
            if isinstance(item, discord.ui.Button) and item.custom_id:
                parts = item.custom_id.split("_", 2)
                if len(parts) >= 2:
                    # custom_id = "act_{action_key}_{guild_id}_{target_id}"
                    action_key_parts = item.custom_id.split("_")
                    # Восстанавливаем action_key (может содержать _)
                    # custom_id формат: act_{action_key}_{guild_id}_{target_id}
                    # Отрезаем последние два числовых сегмента
                    action_key = "_".join(action_key_parts[1:-2])
                    if not has_action_permission(moderator, action_key, cfg):
                        buttons_to_remove.append(item)

        for item in buttons_to_remove:
            view.remove_item(item)

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        # Кэшируем пользователя в БД
        if member:
            await upsert_user(member)


async def setup(bot: commands.Bot):
    await bot.add_cog(ActCog(bot))
