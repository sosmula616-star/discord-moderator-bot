"""
HTML-страницы панели управления (SSR через Jinja2).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from web.routers.deps import require_auth

router = APIRouter()


def get_templates(request: Request):
    return request.app.state.templates


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if "user_id" in request.session:
        return RedirectResponse(url="/dashboard")
    templates = get_templates(request)
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
    })


@router.get("/buttons", response_class=HTMLResponse)
async def buttons_page(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse("buttons.html", {
        "request": request,
        "user": user,
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
    })


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse("reports.html", {
        "request": request,
        "user": user,
    })


@router.get("/punishments", response_class=HTMLResponse)
async def punishments_page(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse("punishments.html", {
        "request": request,
        "user": user,
    })


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse("audit.html", {
        "request": request,
        "user": user,
    })
