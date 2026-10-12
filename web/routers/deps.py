"""
Зависимости FastAPI: авторизация, rate limiting.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict

from fastapi import Request, HTTPException, status


# ─────────────────────────── Auth dependency ───────────────────────────

async def require_auth(request: Request) -> Dict[str, Any]:
    """Проверяет сессию. Возвращает данные пользователя или 401."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/"},
        )
    return {
        "user_id": user_id,
        "username": request.session.get("username", "Unknown"),
        "avatar": request.session.get("avatar"),
        "is_admin": request.session.get("is_admin", False),
        "guild_id": int(request.session.get("guild_id", 0)),
    }


# ─────────────────────────── Rate limiter ───────────────────────────

_rate_store: Dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 60   # запросов
_RATE_WINDOW = 60  # в секундах


async def rate_limit(request: Request) -> None:
    """Простой in-memory rate limiter: 60 запросов / 60 секунд на IP."""
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - _RATE_WINDOW

    # Убираем старые запросы
    _rate_store[ip] = [t for t in _rate_store[ip] if t > window_start]

    if len(_rate_store[ip]) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много запросов. Попробуйте позже.",
        )
    _rate_store[ip].append(now)
