"""
FastAPI бэкенд веб-панели управления.
Запуск: uvicorn web.app:app --reload
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from web.routers import auth, api, panel


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: инициализация БД (создаёт таблицы если не существуют)
    from bot.database import engine, init_db
    await init_db()
    yield
    await engine.dispose()


app = FastAPI(
    title="Discord Moderator Panel",
    description="Веб-панель управления Discord-ботом",
    version="1.0.0",
    lifespan=lifespan,
)

# ─────────────────────────── Middleware ───────────────────────────

SECRET_KEY = os.environ.get("SECRET_KEY", "change_me_please_very_secret_32chars")

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="discord_mod_session",
    max_age=86400,  # 24 часа
    https_only=False,  # True в продакшене с HTTPS
    same_site="lax",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────── Static & Templates ───────────────────────────

BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Делаем templates доступными в роутерах
app.state.templates = templates

# ─────────────────────────── Routers ───────────────────────────

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(panel.router, prefix="", tags=["panel"])
app.include_router(api.router, prefix="/api", tags=["api"])


@app.get("/api/health", tags=["system"])
@app.get("/health", tags=["system"])
async def health_check():
    """Эндпоинт проверки жизнеспособности панели и бота для Bothost / Root."""
    return {
        "status": "online",
        "bot": "warden",
        "client_id": "1558816485760303136",
        "version": "1.0.0",
    }

