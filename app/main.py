from __future__ import annotations

import hmac
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.cloudmail import CloudMailClient
from app.code_extractor import extract_verification_codes
from app.settings import AppSettings
from app.store import KeyStore

_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def create_app(
    settings: AppSettings | None = None,
    store: KeyStore | None = None,
    cloudmail_client: CloudMailClient | Any | None = None,
) -> FastAPI:
    resolved_settings = settings or AppSettings.from_env()
    resolved_store = store or KeyStore(resolved_settings.database_path)
    resolved_cloudmail = cloudmail_client or CloudMailClient(
        base_url=resolved_settings.cloudmail_base_url,
        admin_email=resolved_settings.cloudmail_admin_email,
        admin_password=resolved_settings.cloudmail_admin_password,
        api_token=resolved_settings.cloudmail_api_token,
    )

    app = FastAPI(title=resolved_settings.app_name)
    app.add_middleware(SessionMiddleware, secret_key=resolved_settings.app_secret_key)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    app.state.settings = resolved_settings
    app.state.store = resolved_store
    app.state.cloudmail = resolved_cloudmail

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return _render(request, "index.html", {"error": None})

    @app.post("/lookup")
    def lookup(request: Request, access_key: str = Form(...)) -> Response:
        key = access_key.strip()
        if not key:
            return _render(request, "index.html", {"error": "请输入查看 Key"}, status_code=status.HTTP_400_BAD_REQUEST)
        return RedirectResponse(url=f"/mailbox/{key}", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/mailbox/{access_key}", response_class=HTMLResponse)
    def mailbox(request: Request, access_key: str) -> HTMLResponse:
        mapping = request.app.state.store.get_by_key(access_key)
        if mapping is None:
            return _render(
                request,
                "mailbox.html",
                {"mapping": None, "emails": [], "error": "Key 不存在或已失效"},
                status_code=status.HTTP_404_NOT_FOUND,
            )

        emails = request.app.state.cloudmail.fetch_recent_emails(
            mapping.recipient_email,
            limit=request.app.state.settings.lookup_email_limit,
        )
        rendered_emails = [
            {
                "message": email,
                "codes": extract_verification_codes(email.subject, email.text, email.content),
                "preview": _build_preview(email.text, email.content),
            }
            for email in emails
        ]
        return _render(
            request,
            "mailbox.html",
            {"mapping": mapping, "emails": rendered_emails, "error": None},
        )

    @app.get("/admin/login", response_class=HTMLResponse)
    def admin_login(request: Request) -> HTMLResponse:
        return _render(request, "admin_login.html", {"error": None})

    @app.post("/admin/login")
    def admin_login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ) -> Response:
        if _is_valid_admin_login(request.app.state.settings, username, password):
            request.session["is_admin"] = True
            return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

        return _render(
            request,
            "admin_login.html",
            {"error": "账号或密码错误"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    @app.get("/admin", response_class=HTMLResponse)
    def admin_dashboard(request: Request) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        return _render_admin_dashboard(request)

    @app.post("/admin/keys")
    def admin_create_key(
        request: Request,
        recipient_email: str = Form(...),
        access_key: str = Form(""),
        label: str = Form(""),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            request.app.state.store.create_mapping(
                recipient_email=recipient_email,
                access_key=access_key or None,
                label=label,
            )
        except ValueError as exc:
            return _render_admin_dashboard(request, error=str(exc), status_code=status.HTTP_400_BAD_REQUEST)

        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/admin/logout")
    def admin_logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

    return app


def _render(request: Request, template_name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    merged = {"settings": request.app.state.settings, **context}
    return TEMPLATES.TemplateResponse(request, template_name, merged, status_code=status_code)


def _render_admin_dashboard(request: Request, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    mappings = request.app.state.store.list_mappings()
    return _render(request, "admin_dashboard.html", {"mappings": mappings, "error": error}, status_code=status_code)


def _is_valid_admin_login(settings: AppSettings, username: str, password: str) -> bool:
    return hmac.compare_digest(username, settings.app_admin_username) and hmac.compare_digest(
        password,
        settings.app_admin_password,
    )


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _build_preview(text_value: str, html_value: str) -> str:
    text = (text_value or "").strip()
    if text:
        return text
    stripped = _TAG_PATTERN.sub(" ", html_value or "")
    return _WHITESPACE_PATTERN.sub(" ", stripped).strip()


app = create_app()
