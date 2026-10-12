"""
REST API для веб-панели. Все endpoints требуют авторизации.
Изменения настроек → запись в БД → NOTIFY PostgreSQL → бот обновляет кэш.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, delete, func, and_
from sqlalchemy.orm import selectinload

from bot.database import AsyncSessionLocal
from bot.models import (
    GuildSettings, ButtonConfig, ButtonRole, ButtonStyle,
    Punishment, PunishmentType, Report, ReportStatus, AuditLog,
    PanelAccess, User
)
from bot.utils.helpers import utcnow
from web.routers.deps import require_auth, rate_limit

router = APIRouter()

GUILD_ID = int(os.environ.get("GUILD_ID", 0))


# ─────────────────────────── Notify helper ───────────────────────────

async def notify_bot(guild_id: int) -> None:
    """Отправляет сигнал обновления настроек боту (SQLite / PostgreSQL)."""
    from bot.config_cache import config_cache
    await config_cache.invalidate(guild_id)

    db_url = os.environ.get("DATABASE_URL", "")
    if "postgresql" in db_url:
        dsn = db_url.replace("postgresql+asyncpg://", "postgresql://")
        try:
            conn = await asyncpg.connect(dsn=dsn)
            await conn.execute(
                "SELECT pg_notify($1, $2)",
                "config_update",
                json.dumps({"guild_id": guild_id}),
            )
            await conn.close()
        except Exception:
            pass


async def log_panel_action(
    guild_id: int,
    actor_id: int,
    action: str,
    details: Optional[str] = None,
) -> None:
    """Записывает действие в audit_log и логирует в канал 'логи-панель'."""
    async with AsyncSessionLocal() as session:
        from bot.models import AuditLog
        session.add(AuditLog(
            guild_id=guild_id,
            action=f"panel:{action}",
            actor_id=actor_id,
            details=details,
        ))
        await session.commit()


# ─────────────────────────── Pydantic схемы ───────────────────────────

class ButtonUpdate(BaseModel):
    action_key: str
    label: str
    emoji: Optional[str] = None
    style: str = "secondary"
    order: int = 0
    enabled: bool = True
    role_ids: List[int] = []


class GuildSettingsUpdate(BaseModel):
    role_access_id: Optional[int] = None
    role_noaccess_id: Optional[int] = None
    role_male_id: Optional[int] = None
    role_female_id: Optional[int] = None
    role_ban_id: Optional[int] = None
    role_mute_voice_id: Optional[int] = None
    role_mute_text_id: Optional[int] = None
    role_warn1_id: Optional[int] = None
    role_warn2_id: Optional[int] = None
    role_warning_id: Optional[int] = None
    channel_ban_room_id: Optional[int] = None
    channel_reports_id: Optional[int] = None
    category_reports_id: Optional[int] = None
    channel_log_reports_id: Optional[int] = None
    channel_log_punish_id: Optional[int] = None
    channel_log_verify_id: Optional[int] = None
    channel_log_panel_id: Optional[int] = None
    max_mute_minutes: Optional[int] = None
    auto_ban_warn_count: Optional[int] = None
    auto_ban_days: Optional[int] = None
    report_limit_per_hour: Optional[int] = None


class PanelAccessUpdate(BaseModel):
    role_ids: List[int]


# ─────────────────────────── Endpoints: Settings ───────────────────────────

@router.get("/settings")
async def get_settings(
    user=Depends(require_auth),
    _rl=Depends(rate_limit),
) -> Dict[str, Any]:
    async with AsyncSessionLocal() as session:
        s = await session.get(GuildSettings, GUILD_ID)
        if s is None:
            raise HTTPException(404, "Настройки не найдены")
        return {
            "role_access_id": s.role_access_id,
            "role_noaccess_id": s.role_noaccess_id,
            "role_male_id": s.role_male_id,
            "role_female_id": s.role_female_id,
            "role_ban_id": s.role_ban_id,
            "role_mute_voice_id": s.role_mute_voice_id,
            "role_mute_text_id": s.role_mute_text_id,
            "role_warn1_id": s.role_warn1_id,
            "role_warn2_id": s.role_warn2_id,
            "role_warning_id": s.role_warning_id,
            "channel_ban_room_id": s.channel_ban_room_id,
            "channel_reports_id": s.channel_reports_id,
            "category_reports_id": s.category_reports_id,
            "channel_log_reports_id": s.channel_log_reports_id,
            "channel_log_punish_id": s.channel_log_punish_id,
            "channel_log_verify_id": s.channel_log_verify_id,
            "channel_log_panel_id": s.channel_log_panel_id,
            "max_mute_minutes": s.max_mute_minutes,
            "auto_ban_warn_count": s.auto_ban_warn_count,
            "auto_ban_days": s.auto_ban_days,
            "report_limit_per_hour": s.report_limit_per_hour,
        }


@router.patch("/settings")
async def update_settings(
    data: GuildSettingsUpdate,
    user=Depends(require_auth),
    _rl=Depends(rate_limit),
) -> Dict[str, str]:
    async with AsyncSessionLocal() as session:
        s = await session.get(GuildSettings, GUILD_ID)
        if s is None:
            raise HTTPException(404, "Настройки не найдены")

        update_data = data.model_dump(exclude_none=True)
        for key, value in update_data.items():
            setattr(s, key, value)
        await session.commit()

    await log_panel_action(GUILD_ID, int(user["user_id"]), "settings_update",
                           str(update_data))
    await notify_bot(GUILD_ID)
    return {"status": "ok", "message": "Настройки сохранены"}


# ─────────────────────────── Endpoints: Buttons ───────────────────────────

@router.get("/buttons")
async def get_buttons(
    user=Depends(require_auth),
    _rl=Depends(rate_limit),
) -> List[Dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ButtonConfig)
            .where(ButtonConfig.guild_id == GUILD_ID)
            .options(selectinload(ButtonConfig.roles))
            .order_by(ButtonConfig.order)
        )
        buttons = result.scalars().all()
        return [
            {
                "id": b.id,
                "action_key": b.action_key,
                "label": b.label,
                "emoji": b.emoji,
                "style": b.style.value,
                "order": b.order,
                "enabled": b.enabled,
                "role_ids": [r.role_id for r in b.roles],
            }
            for b in buttons
        ]


@router.put("/buttons")
async def update_buttons(
    data: List[ButtonUpdate],
    user=Depends(require_auth),
    _rl=Depends(rate_limit),
) -> Dict[str, str]:
    async with AsyncSessionLocal() as session:
        for btn_data in data:
            result = await session.execute(
                select(ButtonConfig).where(
                    ButtonConfig.guild_id == GUILD_ID,
                    ButtonConfig.action_key == btn_data.action_key,
                ).options(selectinload(ButtonConfig.roles))
            )
            btn = result.scalar_one_or_none()

            if btn is None:
                btn = ButtonConfig(guild_id=GUILD_ID, action_key=btn_data.action_key)
                session.add(btn)

            btn.label = btn_data.label
            btn.emoji = btn_data.emoji
            try:
                btn.style = ButtonStyle(btn_data.style)
            except ValueError:
                btn.style = ButtonStyle.secondary
            btn.order = btn_data.order
            btn.enabled = btn_data.enabled

            # Обновляем роли
            await session.execute(
                delete(ButtonRole).where(ButtonRole.button_id == btn.id)
                if btn.id else delete(ButtonRole).where(False)  # пустой delete для нового
            )
            await session.flush()

            for role_id in btn_data.role_ids:
                session.add(ButtonRole(button_id=btn.id, role_id=role_id))

        await session.commit()

    await log_panel_action(GUILD_ID, int(user["user_id"]), "buttons_update")
    await notify_bot(GUILD_ID)
    return {"status": "ok", "message": "Кнопки сохранены"}


# ─────────────────────────── Endpoints: Panel Access ───────────────────────────

@router.get("/panel-access")
async def get_panel_access(user=Depends(require_auth), _rl=Depends(rate_limit)) -> List[int]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PanelAccess.role_id).where(PanelAccess.guild_id == GUILD_ID)
        )
        return [row[0] for row in result.all()]


@router.put("/panel-access")
async def update_panel_access(
    data: PanelAccessUpdate,
    user=Depends(require_auth),
    _rl=Depends(rate_limit),
) -> Dict[str, str]:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(PanelAccess).where(PanelAccess.guild_id == GUILD_ID)
        )
        for role_id in data.role_ids:
            session.add(PanelAccess(guild_id=GUILD_ID, role_id=role_id))
        await session.commit()

    await log_panel_action(GUILD_ID, int(user["user_id"]), "panel_access_update",
                           str(data.role_ids))
    await notify_bot(GUILD_ID)
    return {"status": "ok"}


# ─────────────────────────── Endpoints: Punishments ───────────────────────────

@router.get("/punishments")
async def get_punishments(
    user=Depends(require_auth),
    _rl=Depends(rate_limit),
    user_id: Optional[int] = Query(None),
    active: Optional[bool] = Query(None),
    type_filter: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    async with AsyncSessionLocal() as session:
        query = select(Punishment).where(Punishment.guild_id == GUILD_ID)

        if user_id:
            query = query.where(Punishment.user_id == user_id)
        if active is not None:
            query = query.where(Punishment.active == active)
        if type_filter:
            try:
                query = query.where(Punishment.type == PunishmentType(type_filter))
            except ValueError:
                pass

        # Подсчёт
        count_result = await session.execute(
            select(func.count()).select_from(query.subquery())
        )
        total = count_result.scalar_one()

        query = query.order_by(Punishment.created_at.desc())
        query = query.offset((page - 1) * per_page).limit(per_page)

        result = await session.execute(query)
        punishments = result.scalars().all()

        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "items": [
                {
                    "id": p.id,
                    "user_id": p.user_id,
                    "type": p.type.value,
                    "moderator_id": p.moderator_id,
                    "reason": p.reason,
                    "description": p.description,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                    "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                    "active": p.active,
                    "revoked_by": p.revoked_by,
                    "revoked_at": p.revoked_at.isoformat() if p.revoked_at else None,
                    "revoke_reason": p.revoke_reason,
                    "auto": p.auto,
                }
                for p in punishments
            ],
        }


# ─────────────────────────── Endpoints: Reports ───────────────────────────

@router.get("/reports")
async def get_reports(
    user=Depends(require_auth),
    _rl=Depends(rate_limit),
    status_filter: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    async with AsyncSessionLocal() as session:
        query = select(Report).where(Report.guild_id == GUILD_ID)

        if status_filter:
            try:
                query = query.where(Report.status == ReportStatus(status_filter))
            except ValueError:
                pass

        count_result = await session.execute(
            select(func.count()).select_from(query.subquery())
        )
        total = count_result.scalar_one()

        query = query.order_by(Report.created_at.desc())
        query = query.offset((page - 1) * per_page).limit(per_page)

        result = await session.execute(query)
        reports = result.scalars().all()

        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "items": [
                {
                    "id": r.id,
                    "plaintiff_id": r.plaintiff_id,
                    "defendant_id": r.defendant_id,
                    "text": r.text,
                    "evidence": r.evidence,
                    "status": r.status.value,
                    "accepted_by": r.accepted_by,
                    "verdict": r.verdict,
                    "verdict_comment": r.verdict_comment,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                }
                for r in reports
            ],
        }


# ─────────────────────────── Endpoints: Audit Log ───────────────────────────

@router.get("/audit")
async def get_audit(
    user=Depends(require_auth),
    _rl=Depends(rate_limit),
    actor_id: Optional[int] = Query(None),
    target_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    async with AsyncSessionLocal() as session:
        query = select(AuditLog).where(AuditLog.guild_id == GUILD_ID)

        if actor_id:
            query = query.where(AuditLog.actor_id == actor_id)
        if target_id:
            query = query.where(AuditLog.target_id == target_id)

        count_result = await session.execute(
            select(func.count()).select_from(query.subquery())
        )
        total = count_result.scalar_one()

        query = query.order_by(AuditLog.created_at.desc())
        query = query.offset((page - 1) * per_page).limit(per_page)

        result = await session.execute(query)
        logs = result.scalars().all()

        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "items": [
                {
                    "id": al.id,
                    "action": al.action,
                    "actor_id": al.actor_id,
                    "target_id": al.target_id,
                    "punishment_id": al.punishment_id,
                    "report_id": al.report_id,
                    "details": al.details,
                    "created_at": al.created_at.isoformat() if al.created_at else None,
                }
                for al in logs
            ],
        }


# ─────────────────────────── Endpoints: User lookup ───────────────────────────

@router.get("/user/{user_id}")
async def get_user(
    user_id: int,
    user=Depends(require_auth),
    _rl=Depends(rate_limit),
) -> Dict[str, Any]:
    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user_id)
        if db_user is None:
            raise HTTPException(404, f"Пользователь {user_id} не найден в базе")

        # Наказания
        result = await session.execute(
            select(Punishment).where(
                Punishment.user_id == user_id,
                Punishment.guild_id == GUILD_ID,
            ).order_by(Punishment.created_at.desc()).limit(50)
        )
        punishments = result.scalars().all()

        # Жалобы
        report_result = await session.execute(
            select(Report).where(
                Report.defendant_id == user_id,
                Report.guild_id == GUILD_ID,
            ).order_by(Report.created_at.desc()).limit(20)
        )
        reports = report_result.scalars().all()

    return {
        "user": {
            "id": db_user.id,
            "username": db_user.username,
            "display_name": db_user.display_name,
            "avatar_url": db_user.avatar_url,
            "gender": db_user.gender,
            "access_status": db_user.access_status,
            "first_seen": db_user.first_seen.isoformat() if db_user.first_seen else None,
            "joined_at": db_user.joined_at.isoformat() if db_user.joined_at else None,
        },
        "punishments": [
            {
                "id": p.id,
                "type": p.type.value,
                "reason": p.reason,
                "active": p.active,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "expires_at": p.expires_at.isoformat() if p.expires_at else None,
            }
            for p in punishments
        ],
        "reports": [
            {
                "id": r.id,
                "plaintiff_id": r.plaintiff_id,
                "status": r.status.value,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reports
        ],
    }


# ─────────────────────────── Ban role sync ───────────────────────────

@router.post("/sync-ban-permissions")
async def sync_ban_permissions(
    user=Depends(require_auth),
    _rl=Depends(rate_limit),
) -> Dict[str, str]:
    """Инициирует пересинхронизацию прав бан-роли через NOTIFY."""
    await notify_bot(GUILD_ID)
    await log_panel_action(GUILD_ID, int(user["user_id"]), "sync_ban_permissions")
    return {"status": "ok", "message": "Сигнал синхронизации отправлен боту"}
