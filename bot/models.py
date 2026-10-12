"""
Модели SQLAlchemy и конфигурация БД.
Все таблицы описаны здесь; Alembic читает metadata отсюда.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey,
    Integer, String, Text, func, Index, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncAttrs


class Base(AsyncAttrs, DeclarativeBase):
    pass


# ─────────────────────────── Enums ───────────────────────────

class PunishmentType(str, enum.Enum):
    ban_role   = "ban_role"
    mute_voice = "mute_voice"
    mute_text  = "mute_text"
    warn       = "warn"
    warning    = "warning"   # краткосрочное предупреждение (1 или 6 ч)


class ReportStatus(str, enum.Enum):
    open      = "open"
    accepted  = "accepted"
    rejected  = "rejected"
    closed    = "closed"


class ButtonStyle(str, enum.Enum):
    primary   = "primary"    # синий
    secondary = "secondary"  # серый
    success   = "success"    # зелёный
    danger    = "danger"     # красный


# ─────────────────────────── GuildSettings ───────────────────────────

class GuildSettings(Base):
    """Основные настройки сервера (одна строка на гильдию)."""
    __tablename__ = "guild_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Роли верификации
    role_access_id:    Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)   # «Допуск»
    role_noaccess_id:  Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)   # «Недопуск»
    role_male_id:      Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)   # «♂ Мужской»
    role_female_id:    Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)   # «♀ Женский»

    # Роли наказаний
    role_ban_id:           Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    role_mute_voice_id:    Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    role_mute_text_id:     Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    role_warn1_id:         Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # «Варн 1/3»
    role_warn2_id:         Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # «Варн 2/3»
    role_warning_id:       Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # «Предупреждение»

    # Каналы и категории
    channel_ban_room_id:     Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    channel_reports_id:      Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_reports_id:     Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_logs_id:        Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    channel_log_reports_id:  Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    channel_log_punish_id:   Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    channel_log_verify_id:   Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    channel_log_panel_id:    Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Лимиты
    max_mute_minutes:      Mapped[int] = mapped_column(Integer, default=1440)
    auto_ban_warn_count:   Mapped[int] = mapped_column(Integer, default=3)
    auto_ban_days:         Mapped[int] = mapped_column(Integer, default=30)
    report_limit_per_hour: Mapped[int] = mapped_column(Integer, default=5)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ─────────────────────────── ButtonConfig ───────────────────────────

class ButtonConfig(Base):
    """Настройка кнопок команды /act."""
    __tablename__ = "button_config"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id:   Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action_key: Mapped[str] = mapped_column(String(64), nullable=False)   # напр. "ban", "mute_voice"
    label:      Mapped[str] = mapped_column(String(80), nullable=False)
    emoji:      Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    style:      Mapped[ButtonStyle] = mapped_column(Enum(ButtonStyle), default=ButtonStyle.secondary)
    order:      Mapped[int] = mapped_column(Integer, default=0)
    enabled:    Mapped[bool] = mapped_column(Boolean, default=True)

    roles: Mapped[List["ButtonRole"]] = relationship(
        "ButtonRole", back_populates="button", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("guild_id", "action_key", name="uq_button_guild_action"),
    )


class ButtonRole(Base):
    """Роли, которым доступна кнопка."""
    __tablename__ = "button_roles"

    id:        Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    button_id: Mapped[int] = mapped_column(Integer, ForeignKey("button_config.id", ondelete="CASCADE"))
    role_id:   Mapped[int] = mapped_column(BigInteger, nullable=False)

    button: Mapped["ButtonConfig"] = relationship("ButtonConfig", back_populates="roles")


# ─────────────────────────── Users ───────────────────────────

class User(Base):
    """Кэш пользователей сервера."""
    __tablename__ = "users"

    id:            Mapped[int]           = mapped_column(BigInteger, primary_key=True)  # Discord user ID
    guild_id:      Mapped[int]           = mapped_column(BigInteger, nullable=False, index=True)
    username:      Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    display_name:  Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    avatar_url:    Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    gender:        Mapped[Optional[str]] = mapped_column(String(16), nullable=True)     # "male"/"female"/None
    access_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)    # "access"/"noaccess"/None
    first_seen:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    joined_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    left_at:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    punishments: Mapped[List["Punishment"]] = relationship("Punishment", back_populates="user", foreign_keys="Punishment.user_id")

    __table_args__ = (
        UniqueConstraint("id", "guild_id", name="uq_user_guild"),
    )


# ─────────────────────────── Punishments ───────────────────────────

class Punishment(Base):
    """Наказание пользователя."""
    __tablename__ = "punishments"

    id:           Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id:     Mapped[int]          = mapped_column(BigInteger, nullable=False, index=True)
    user_id:      Mapped[int]          = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    type:         Mapped[PunishmentType] = mapped_column(Enum(PunishmentType), nullable=False)
    moderator_id: Mapped[int]          = mapped_column(BigInteger, nullable=False)
    reason:       Mapped[str]          = mapped_column(String(512), nullable=False)
    description:  Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:   Mapped[datetime]     = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    active:       Mapped[bool]         = mapped_column(Boolean, default=True)
    revoked_by:   Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    revoked_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    auto:         Mapped[bool]         = mapped_column(Boolean, default=False)  # автоматическое наказание

    user: Mapped["User"] = relationship("User", back_populates="punishments", foreign_keys=[user_id])
    role_backups: Mapped[List["RoleBackup"]] = relationship(
        "RoleBackup", back_populates="punishment", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_punishments_user_active", "user_id", "active"),
        Index("ix_punishments_expires", "expires_at", "active"),
        Index("ix_punishments_guild", "guild_id"),
    )


class RoleBackup(Base):
    """Сохранённые роли пользователя перед баном."""
    __tablename__ = "role_backups"

    id:            Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    punishment_id: Mapped[int] = mapped_column(Integer, ForeignKey("punishments.id", ondelete="CASCADE"))
    user_id:       Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    role_id:       Mapped[int] = mapped_column(BigInteger, nullable=False)

    punishment: Mapped["Punishment"] = relationship("Punishment", back_populates="role_backups")


# ─────────────────────────── Reports ───────────────────────────

class Report(Base):
    """Жалоба."""
    __tablename__ = "reports"

    id:              Mapped[int]             = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id:        Mapped[int]             = mapped_column(BigInteger, nullable=False, index=True)
    plaintiff_id:    Mapped[int]             = mapped_column(BigInteger, nullable=False)   # истец
    defendant_id:    Mapped[int]             = mapped_column(BigInteger, nullable=False)   # нарушитель
    text:            Mapped[str]             = mapped_column(Text, nullable=False)
    evidence:        Mapped[Optional[str]]   = mapped_column(Text, nullable=True)
    status:          Mapped[ReportStatus]    = mapped_column(Enum(ReportStatus), default=ReportStatus.open)
    accepted_by:     Mapped[Optional[int]]   = mapped_column(BigInteger, nullable=True)
    voice_channel_id: Mapped[Optional[int]]  = mapped_column(BigInteger, nullable=True)
    report_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # embed в канале жалоб
    control_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True) # управление в войс-чате
    verdict:         Mapped[Optional[str]]   = mapped_column(String(32), nullable=True)   # "confirmed"/"rejected"
    verdict_comment: Mapped[Optional[str]]   = mapped_column(Text, nullable=True)
    created_at:      Mapped[datetime]        = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    moves: Mapped[List["ReportMove"]] = relationship(
        "ReportMove", back_populates="report", cascade="all, delete-orphan"
    )


class ReportMove(Base):
    """Запомненные каналы пользователей для возврата после репорта."""
    __tablename__ = "report_moves"

    id:               Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id:        Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"))
    user_id:          Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    report: Mapped["Report"] = relationship("Report", back_populates="moves")


# ─────────────────────────── AuditLog ───────────────────────────

class AuditLog(Base):
    """Журнал всех действий."""
    __tablename__ = "audit_log"

    id:           Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id:     Mapped[int]          = mapped_column(BigInteger, nullable=False, index=True)
    action:       Mapped[str]          = mapped_column(String(128), nullable=False)
    actor_id:     Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    target_id:    Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    punishment_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    report_id:    Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:   Mapped[datetime]     = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ─────────────────────────── PanelAccess ───────────────────────────

class PanelAccess(Base):
    """Роли, которым разрешён доступ к веб-панели."""
    __tablename__ = "panel_access"

    id:       Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    role_id:  Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("guild_id", "role_id", name="uq_panel_access"),
    )


# ─────────────────────────── ReportSpam ───────────────────────────

class ReportSpam(Base):
    """Антиспам: сколько раз пользователь подал жалобу за последний час."""
    __tablename__ = "report_spam"

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id:   Mapped[int]      = mapped_column(BigInteger, nullable=False)
    user_id:    Mapped[int]      = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_report_spam_user_time", "guild_id", "user_id", "created_at"),
    )
