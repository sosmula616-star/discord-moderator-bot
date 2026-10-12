"""
Discord OAuth2 авторизация.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse

router = APIRouter()

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "http://localhost:8000/auth/callback")
DISCORD_API = "https://discord.com/api/v10"

OAUTH2_URL = (
    f"https://discord.com/api/oauth2/authorize"
    f"?client_id={DISCORD_CLIENT_ID}"
    f"&redirect_uri={DISCORD_REDIRECT_URI}"
    f"&response_type=code"
    f"&scope=identify%20guilds%20guilds.members.read"
)


@router.get("/login")
async def login(request: Request):
    """Перенаправляет на Discord OAuth2."""
    return RedirectResponse(url=OAUTH2_URL)


@router.get("/callback")
async def callback(request: Request, code: Optional[str] = None, error: Optional[str] = None):
    """Обрабатывает callback от Discord."""
    if error or not code:
        return RedirectResponse(url="/?error=oauth_failed")

    async with httpx.AsyncClient() as client:
        # Получаем токен
        token_response = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_response.status_code != 200:
            return RedirectResponse(url="/?error=token_failed")

        token_data = token_response.json()
        access_token = token_data.get("access_token")

        # Получаем информацию о пользователе
        user_response = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_response.status_code != 200:
            return RedirectResponse(url="/?error=user_failed")

        user_data = user_response.json()

        # Получаем гильдии пользователя
        guilds_response = await client.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        guilds = guilds_response.json() if guilds_response.status_code == 200 else []

    guild_id = int(os.environ.get("GUILD_ID", 0))
    owner_ids = [s.strip() for s in os.environ.get("OWNER_IDS", "").split(",") if s.strip()]

    # Проверяем, является ли пользователь владельцем Root ovrdose
    is_owner = str(user_data.get("id")) in owner_ids

    # Проверяем, является ли пользователь администратором нужного сервера
    is_admin = is_owner
    guild_data = None
    for g in guilds:
        if int(g["id"]) == guild_id:
            guild_data = g
            # permissions & 0x8 = ADMINISTRATOR
            if int(g.get("permissions", 0)) & 0x8:
                is_admin = True
            break

    # Если не администратор и не владелец — проверяем роли из panel_access
    has_panel_role = False
    if not is_admin and guild_data:
        has_panel_role = await _check_panel_roles(
            access_token, guild_id, int(user_data["id"])
        )

    if not is_admin and not has_panel_role:
        return RedirectResponse(url="/?error=no_access")

    # Сохраняем сессию
    request.session["user_id"] = user_data["id"]
    request.session["username"] = user_data.get("username", "Unknown")
    request.session["avatar"] = user_data.get("avatar")
    request.session["is_admin"] = is_admin
    request.session["access_token"] = access_token
    request.session["guild_id"] = str(guild_id)

    return RedirectResponse(url="/dashboard")


async def _check_panel_roles(access_token: str, guild_id: int, user_id: int) -> bool:
    """Проверяет роли пользователя против panel_access."""
    from bot.database import AsyncSessionLocal
    from bot.models import PanelAccess
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PanelAccess.role_id).where(PanelAccess.guild_id == guild_id)
        )
        allowed_role_ids = {row[0] for row in result.all()}

    if not allowed_role_ids:
        return False

    # Получаем роли пользователя на сервере через Discord API
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://discord.com/api/v10/users/@me/guilds/{guild_id}/member",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            return False
        member_data = resp.json()

    user_role_ids = {int(r) for r in member_data.get("roles", [])}
    return bool(user_role_ids & allowed_role_ids)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")
