"""
Cog: /report — система жалоб с голосовыми комнатами.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, func, and_

from bot.config_cache import config_cache
from bot.database import AsyncSessionLocal
from bot.models import (
    Report, ReportStatus, ReportMove, GuildSettings, AuditLog
)
from bot.utils.helpers import audit, send_log, safe_dm, utcnow, format_dt

log = logging.getLogger(__name__)


# ─────────────────────────── Модал жалобы ───────────────────────────

class ReportModal(discord.ui.Modal, title="Подача жалобы"):
    text = discord.ui.TextInput(
        label="Суть жалобы",
        style=discord.TextStyle.paragraph,
        placeholder="Опишите нарушение подробно",
        max_length=2000,
        required=True,
    )
    evidence = discord.ui.TextInput(
        label="Доказательства (ссылки/текст)",
        style=discord.TextStyle.paragraph,
        placeholder="Скриншоты, ссылки на сообщения...",
        max_length=2000,
        required=False,
    )

    def __init__(self, defendant_id: int):
        super().__init__()
        self.defendant_id = defendant_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        plaintiff = interaction.user
        cfg = await config_cache.get_or_load(guild.id)

        # Антиспам
        one_hour_ago = utcnow() - timedelta(hours=1)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(func.count()).where(
                    and_(
                        Report.plaintiff_id == plaintiff.id,
                        Report.guild_id == guild.id,
                        Report.created_at >= one_hour_ago,
                    )
                )
            )
            recent_count = result.scalar_one()

        if recent_count >= cfg.report_limit_per_hour:
            await interaction.followup.send(
                f"❌ Превышен лимит жалоб: не более {cfg.report_limit_per_hour} в час.",
                ephemeral=True,
            )
            return

        # Создаём репорт
        async with AsyncSessionLocal() as session:
            report = Report(
                guild_id=guild.id,
                plaintiff_id=plaintiff.id,
                defendant_id=self.defendant_id,
                text=self.text.value,
                evidence=self.evidence.value or None,
                status=ReportStatus.open,
            )
            session.add(report)
            await session.commit()
            report_id = report.id

        await audit(guild.id, "report_create", actor_id=plaintiff.id,
                    target_id=self.defendant_id, report_id=report_id)

        # Отправляем embed в канал жалоб
        reports_channel_id = cfg.channel_reports_id
        reports_channel = guild.get_channel(reports_channel_id) if reports_channel_id else None

        if reports_channel is None:
            reports_channel = await self._get_or_create_reports_channel(guild, cfg)

        embed = await build_report_embed(report_id, guild, interaction.client)
        view = ReportActionView(report_id)

        try:
            msg = await reports_channel.send(embed=embed, view=view)
            async with AsyncSessionLocal() as session:
                r = await session.get(Report, report_id)
                r.report_message_id = msg.id
                await session.commit()
        except (discord.Forbidden, discord.HTTPException) as e:
            log.error(f"ReportModal: не удалось отправить в канал жалоб: {e}")

        # Лог
        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            ch_id = s.channel_log_reports_id if s else None
        log_embed = discord.Embed(
            title=f"📋 Репорт #{report_id} подан",
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        log_embed.add_field(name="Истец", value=f"{plaintiff.mention} (`{plaintiff.id}`)", inline=True)
        log_embed.add_field(name="Ответчик", value=f"<@{self.defendant_id}>", inline=True)
        log_embed.add_field(name="Суть", value=self.text.value[:512], inline=False)
        await send_log(interaction.client, ch_id, log_embed)

        await interaction.followup.send(
            f"✅ Жалоба подана, ваш номер: **#{report_id}**. Модераторы рассмотрят её в ближайшее время.",
            ephemeral=True,
        )

    async def _get_or_create_reports_channel(
        self, guild: discord.Guild, cfg
    ) -> discord.TextChannel:
        """Создаёт приватный канал жалоб, если его нет."""
        # Находим роли модераторов из button_roles для любой кнопки
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        channel = await guild.create_text_channel(
            name="жалобы",
            overwrites=overwrites,
            reason="Автоматическое создание канала жалоб",
        )
        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            if s:
                s.channel_reports_id = channel.id
            await session.commit()
        return channel


# ─────────────────────────── Embed жалобы ───────────────────────────

async def build_report_embed(report_id: int, guild: discord.Guild, bot: discord.Client) -> discord.Embed:
    async with AsyncSessionLocal() as session:
        report = await session.get(Report, report_id)
        if report is None:
            return discord.Embed(title="Репорт не найден", color=discord.Color.red())

    status_colors = {
        ReportStatus.open:     discord.Color.yellow(),
        ReportStatus.accepted: discord.Color.green(),
        ReportStatus.rejected: discord.Color.red(),
        ReportStatus.closed:   discord.Color.greyple(),
    }
    status_labels = {
        ReportStatus.open:     "🟡 Открыта",
        ReportStatus.accepted: "🟢 В работе",
        ReportStatus.rejected: "🔴 Отклонена",
        ReportStatus.closed:   "⚫ Закрыта",
    }

    embed = discord.Embed(
        title=f"📋 Жалоба #{report.id}",
        color=status_colors.get(report.status, discord.Color.blurple()),
        timestamp=report.created_at,
    )
    embed.add_field(name="Статус", value=status_labels.get(report.status, str(report.status)), inline=True)
    embed.add_field(name="Истец", value=f"<@{report.plaintiff_id}>", inline=True)
    embed.add_field(name="Ответчик", value=f"<@{report.defendant_id}>", inline=True)
    embed.add_field(name="Суть жалобы", value=report.text[:1000], inline=False)
    if report.evidence:
        embed.add_field(name="Доказательства", value=report.evidence[:500], inline=False)
    if report.accepted_by:
        embed.add_field(name="Принял", value=f"<@{report.accepted_by}>", inline=True)
    if report.verdict:
        verdict_str = "✅ Нарушение подтверждено" if report.verdict == "confirmed" else "❌ Нарушение не подтверждено"
        embed.add_field(name="Вердикт", value=verdict_str, inline=True)
        if report.verdict_comment:
            embed.add_field(name="Комментарий", value=report.verdict_comment[:500], inline=False)
    return embed


# ─────────────────────────── Views жалоб ───────────────────────────

class RejectReasonModal(discord.ui.Modal, title="Отклонение жалобы"):
    reason = discord.ui.TextInput(
        label="Причина отклонения", max_length=512, required=True
    )

    def __init__(self, report_id: int):
        super().__init__()
        self.report_id = report_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.guild

        async with AsyncSessionLocal() as session:
            report = await session.get(Report, self.report_id)
            if not report or report.status != ReportStatus.open:
                await interaction.followup.send("❌ Жалоба уже обработана.", ephemeral=True)
                return
            report.status = ReportStatus.rejected
            report.verdict = "rejected"
            report.verdict_comment = self.reason.value
            report.closed_at = utcnow()
            await session.commit()

        await audit(guild.id, "report_reject", actor_id=interaction.user.id,
                    report_id=self.report_id, details=self.reason.value)

        embed = await build_report_embed(self.report_id, guild, interaction.client)
        await interaction.message.edit(embed=embed, view=None)

        # DM истцу
        async with AsyncSessionLocal() as session:
            report = await session.get(Report, self.report_id)

        dm_embed = discord.Embed(
            title=f"Жалоба #{self.report_id} отклонена",
            description=f"**Причина:** {self.reason.value}",
            color=discord.Color.red(),
        )
        try:
            plaintiff = await interaction.client.fetch_user(report.plaintiff_id)
            await safe_dm(plaintiff, dm_embed)
        except Exception:
            pass

        # Лог
        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            ch_id = s.channel_log_reports_id if s else None
        log_embed = discord.Embed(
            title=f"📋 Репорт #{self.report_id} отклонён",
            color=discord.Color.red(), timestamp=utcnow()
        )
        log_embed.add_field(name="Модератор", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Причина", value=self.reason.value, inline=False)
        await send_log(interaction.client, ch_id, log_embed)
        await interaction.followup.send("✅ Жалоба отклонена.", ephemeral=True)


class ReportActionView(discord.ui.View):
    """Кнопки «Принять» / «Отклонить» в канале жалоб. Persistent."""

    def __init__(self, report_id: int):
        super().__init__(timeout=None)
        self.report_id = report_id

    @discord.ui.button(
        label="✅ Принять", style=discord.ButtonStyle.success,
        custom_id="report_accept"
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        guild = interaction.guild

        async with AsyncSessionLocal() as session:
            report = await session.get(Report, self.report_id)
            if not report or report.status != ReportStatus.open:
                await interaction.followup.send("❌ Жалоба уже обработана.", ephemeral=True)
                return
            report.status = ReportStatus.accepted
            report.accepted_by = interaction.user.id
            await session.commit()

        await audit(guild.id, "report_accept", actor_id=interaction.user.id, report_id=self.report_id)

        # Обновляем embed
        embed = await build_report_embed(self.report_id, guild, interaction.client)
        await interaction.message.edit(embed=embed, view=None)

        # Создаём голосовой канал
        cfg = await config_cache.get_or_load(guild.id)
        voice_channel = await self._create_voice_channel(guild, cfg, report, interaction.user)

        if voice_channel:
            async with AsyncSessionLocal() as session:
                r = await session.get(Report, self.report_id)
                r.voice_channel_id = voice_channel.id
                await session.commit()

            # Отправляем меню управления в текстовый чат канала
            ctrl_embed = discord.Embed(
                title=f"🎙 Управление репортом #{self.report_id}",
                description="Меню управления для модераторов",
                color=discord.Color.blurple(),
            )
            ctrl_view = ReportControlView(self.report_id)
            ctrl_msg = await voice_channel.send(embed=ctrl_embed, view=ctrl_view)

            async with AsyncSessionLocal() as session:
                r = await session.get(Report, self.report_id)
                r.control_message_id = ctrl_msg.id
                await session.commit()

        # Лог
        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            ch_id = s.channel_log_reports_id if s else None
        log_embed = discord.Embed(
            title=f"📋 Репорт #{self.report_id} принят",
            color=discord.Color.green(), timestamp=utcnow()
        )
        log_embed.add_field(name="Модератор", value=interaction.user.mention, inline=True)
        if voice_channel:
            log_embed.add_field(name="Голос. канал", value=voice_channel.mention, inline=True)
        await send_log(interaction.client, ch_id, log_embed)
        await interaction.followup.send(
            f"✅ Жалоба принята. Создан канал {voice_channel.mention if voice_channel else '(ошибка)'}.",
            ephemeral=True
        )

    @discord.ui.button(
        label="❌ Отклонить", style=discord.ButtonStyle.danger,
        custom_id="report_reject"
    )
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectReasonModal(self.report_id))

    async def _create_voice_channel(
        self,
        guild: discord.Guild,
        cfg,
        report: Report,
        moderator: discord.Member,
    ) -> Optional[discord.VoiceChannel]:
        # Категория репортов
        category = guild.get_channel(cfg.category_reports_id) if cfg.category_reports_id else None
        if category is None:
            try:
                category = await guild.create_category(
                    name="Репорты",
                    reason="Автоматическое создание категории репортов",
                )
                async with AsyncSessionLocal() as session:
                    s = await session.get(GuildSettings, guild.id)
                    if s:
                        s.category_reports_id = category.id
                    await session.commit()
            except (discord.Forbidden, discord.HTTPException) as e:
                log.error(f"Не удалось создать категорию Репорты: {e}")
                return None

        # Права доступа
        plaintiff = guild.get_member(report.plaintiff_id)
        defendant = guild.get_member(report.defendant_id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, manage_channels=True),
            moderator: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
        }
        if plaintiff:
            overwrites[plaintiff] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
        if defendant:
            overwrites[defendant] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)

        try:
            channel = await guild.create_voice_channel(
                name=f"Репорт #{report.id}",
                category=category,
                overwrites=overwrites,
                reason=f"Репорт #{report.id}",
            )
            # Сохраняем исходные каналы
            async with AsyncSessionLocal() as session:
                for user in [plaintiff, defendant]:
                    if user and user.voice:
                        session.add(ReportMove(
                            report_id=report.id,
                            user_id=user.id,
                            origin_channel_id=user.voice.channel.id,
                        ))
                await session.commit()
            return channel
        except (discord.Forbidden, discord.HTTPException) as e:
            log.error(f"Не удалось создать голосовой канал репорта: {e}")
            return None


# ─────────────────────────── Вердикт модал ───────────────────────────

class VerdictModal(discord.ui.Modal):
    comment = discord.ui.TextInput(
        label="Комментарий к вердикту",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    def __init__(self, report_id: int, verdict: str):
        super().__init__(title="Вердикт по репорту")
        self.report_id = report_id
        self.verdict = verdict  # "confirmed" или "rejected"

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.guild

        async with AsyncSessionLocal() as session:
            report = await session.get(Report, self.report_id)
            if not report:
                await interaction.followup.send("❌ Репорт не найден.", ephemeral=True)
                return
            report.status = ReportStatus.closed
            report.verdict = self.verdict
            report.verdict_comment = self.comment.value
            report.closed_at = utcnow()
            voice_channel_id = report.voice_channel_id
            plaintiff_id = report.plaintiff_id
            await session.commit()

        await audit(guild.id, f"report_verdict_{self.verdict}",
                    actor_id=interaction.user.id, report_id=self.report_id,
                    details=self.comment.value)

        # DM истцу
        verdict_str = "✅ Нарушение подтверждено" if self.verdict == "confirmed" else "❌ Нарушение не подтверждено"
        dm_embed = discord.Embed(
            title=f"Итог по жалобе #{self.report_id}",
            description=f"**Вердикт:** {verdict_str}\n**Комментарий:** {self.comment.value}",
            color=discord.Color.green() if self.verdict == "confirmed" else discord.Color.red(),
        )
        try:
            plaintiff = await interaction.client.fetch_user(plaintiff_id)
            await safe_dm(plaintiff, dm_embed)
        except Exception:
            pass

        # Возвращаем участников
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ReportMove).where(ReportMove.report_id == self.report_id)
            )
            moves = result.scalars().all()

        for move in moves:
            member = guild.get_member(move.user_id)
            if member and member.voice and move.origin_channel_id:
                origin = guild.get_channel(move.origin_channel_id)
                if origin:
                    try:
                        await member.move_to(origin, reason="Завершение репорта")
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        # Удаляем голосовой канал через 60 секунд
        if voice_channel_id:
            voice_ch = guild.get_channel(voice_channel_id)
            if voice_ch:
                await interaction.followup.send(
                    "✅ Вердикт вынесен. Канал будет удалён через 60 секунд.", ephemeral=True
                )
                await asyncio.sleep(60)
                try:
                    await voice_ch.delete(reason="Репорт закрыт")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        else:
            await interaction.followup.send("✅ Вердикт вынесен.", ephemeral=True)

        # Лог
        async with AsyncSessionLocal() as session:
            s = await session.get(GuildSettings, guild.id)
            ch_id = s.channel_log_reports_id if s else None
        log_embed = discord.Embed(
            title=f"📋 Репорт #{self.report_id} закрыт",
            color=discord.Color.green() if self.verdict == "confirmed" else discord.Color.red(),
            timestamp=utcnow(),
        )
        log_embed.add_field(name="Модератор", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Вердикт", value=verdict_str, inline=True)
        log_embed.add_field(name="Комментарий", value=self.comment.value[:500], inline=False)
        await send_log(interaction.client, ch_id, log_embed)


# ─────────────────────────── Меню управления в голосовом канале ───────────────────────────

class ReportControlView(discord.ui.View):
    """Persistent view в текстовом чате голосового канала репорта."""

    def __init__(self, report_id: int):
        super().__init__(timeout=None)
        self.report_id = report_id

    async def _check_mod(self, interaction: discord.Interaction) -> bool:
        """Проверяет права модератора."""
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "❌ Только модераторы могут управлять репортом.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="📞 Переместить истца", style=discord.ButtonStyle.secondary,
                       custom_id="report_move_plaintiff")
    async def move_plaintiff(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_mod(interaction):
            return
        await self._move_user(interaction, "plaintiff")

    @discord.ui.button(label="📞 Переместить ответчика", style=discord.ButtonStyle.secondary,
                       custom_id="report_move_defendant")
    async def move_defendant(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_mod(interaction):
            return
        await self._move_user(interaction, "defendant")

    @discord.ui.button(label="✅ Принять репорт", style=discord.ButtonStyle.success,
                       custom_id="report_verdict_confirm")
    async def confirm_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_mod(interaction):
            return
        await interaction.response.send_modal(VerdictModal(self.report_id, "confirmed"))

    @discord.ui.button(label="❌ Отклонить репорт", style=discord.ButtonStyle.danger,
                       custom_id="report_verdict_reject")
    async def reject_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_mod(interaction):
            return
        await interaction.response.send_modal(VerdictModal(self.report_id, "rejected"))

    async def _move_user(self, interaction: discord.Interaction, role: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        async with AsyncSessionLocal() as session:
            report = await session.get(Report, self.report_id)
            if not report:
                await interaction.followup.send("❌ Репорт не найден.", ephemeral=True)
                return
            user_id = report.plaintiff_id if role == "plaintiff" else report.defendant_id
            voice_ch_id = report.voice_channel_id

        member = guild.get_member(user_id)
        if member is None:
            await interaction.followup.send("❌ Пользователь не на сервере.", ephemeral=True)
            return
        if not member.voice:
            name = "Истец" if role == "plaintiff" else "Ответчик"
            await interaction.followup.send(f"❌ {name} не в голосовом канале.", ephemeral=True)
            return

        voice_ch = guild.get_channel(voice_ch_id) if voice_ch_id else None
        if not voice_ch:
            await interaction.followup.send("❌ Голосовой канал репорта не найден.", ephemeral=True)
            return

        # Сохраняем исходный канал
        async with AsyncSessionLocal() as session:
            existing = await session.execute(
                select(ReportMove).where(
                    ReportMove.report_id == self.report_id,
                    ReportMove.user_id == user_id,
                )
            )
            existing_move = existing.scalar_one_or_none()
            if not existing_move:
                session.add(ReportMove(
                    report_id=self.report_id,
                    user_id=user_id,
                    origin_channel_id=member.voice.channel.id,
                ))
                await session.commit()

        try:
            await member.move_to(voice_ch, reason=f"Репорт #{self.report_id}: перемещение")
            name = "Истец" if role == "plaintiff" else "Ответчик"
            await interaction.followup.send(f"✅ {name} перемещён.", ephemeral=True)

            await audit(guild.id, f"report_move_{role}", actor_id=interaction.user.id,
                        target_id=user_id, report_id=self.report_id)
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав для перемещения.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


# ─────────────────────────── Cog ───────────────────────────

class ReportCog(commands.Cog, name="Report"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """Восстанавливает persistent views при загрузке."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Report).where(
                    Report.status.in_([ReportStatus.open, ReportStatus.accepted])
                )
            )
            active_reports = result.scalars().all()

        for report in active_reports:
            if report.status == ReportStatus.open:
                self.bot.add_view(ReportActionView(report.id))
            if report.voice_channel_id and report.control_message_id:
                self.bot.add_view(ReportControlView(report.id))

        log.info(f"ReportCog: восстановлено {len(active_reports)} активных репортов")

    @app_commands.command(name="report", description="Подать жалобу на пользователя")
    @app_commands.describe(user="Нарушитель (участник или ID)")
    @app_commands.guild_only()
    async def report(self, interaction: discord.Interaction, user: str):
        # Парсим ID
        clean = user.strip().lstrip("<@!").rstrip(">")
        try:
            defendant_id = int(clean)
        except ValueError:
            await interaction.response.send_message(
                "❌ Укажите ID пользователя или упоминание.", ephemeral=True
            )
            return

        if defendant_id == interaction.user.id:
            await interaction.response.send_message(
                "❌ Нельзя подавать жалобу на самого себя.", ephemeral=True
            )
            return

        await interaction.response.send_modal(ReportModal(defendant_id))


async def setup(bot: commands.Bot):
    await bot.add_cog(ReportCog(bot))
