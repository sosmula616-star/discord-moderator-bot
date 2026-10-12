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
    return templates.TemplateResponse(request=request, name="login.html")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"user": user})


@router.get("/buttons", response_class=HTMLResponse)
async def buttons_page(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse(request=request, name="buttons.html", context={"user": user})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse(request=request, name="settings.html", context={"user": user})


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse(request=request, name="reports.html", context={"user": user})


@router.get("/punishments", response_class=HTMLResponse)
async def punishments_page(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse(request=request, name="punishments.html", context={"user": user})


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, user=Depends(require_auth)):
    templates = get_templates(request)
    return templates.TemplateResponse(request=request, name="audit.html", context={"user": user})

