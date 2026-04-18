from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.cloudmail import CloudMailClient, CloudMailError
from app.code_extractor import extract_verification_codes
from app.settings import AppSettings
from app.store import CloudMailSettingsRecord, KeyStore

_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@dataclass(slots=True)
class ResolvedCloudMailConfig:
    base_url: str
    api_token: str | None = None
    admin_email: str | None = None
    admin_password: str | None = None


def create_app(
    settings: AppSettings | None = None,
    store: KeyStore | None = None,
    cloudmail_client: Any | None = None,
    cloudmail_client_factory: Callable[[ResolvedCloudMailConfig], Any] | None = None,
) -> FastAPI:
    resolved_settings = settings or AppSettings.from_env()
    resolved_store = store or KeyStore(resolved_settings.database_path)

    app = FastAPI(title=resolved_settings.app_name)
    app.add_middleware(SessionMiddleware, secret_key=resolved_settings.app_secret_key)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    app.state.settings = resolved_settings
    app.state.store = resolved_store
    app.state.fixed_cloudmail_client = cloudmail_client
    app.state.cloudmail_client_factory = cloudmail_client_factory

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return _render(request, "index.html", {"error": None, "header_centered": True})

    @app.post("/lookup")
    def lookup(request: Request, access_key: str = Form(...)) -> Response:
        key = access_key.strip()
        if not key:
            return _render(
                request,
                "index.html",
                {"error": "请输入查看 Key", "header_centered": True},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
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

        try:
            emails = _get_cloudmail_client(request).fetch_recent_emails(
                mapping.query_email,
                limit=request.app.state.settings.lookup_email_limit,
            )
        except CloudMailError as exc:
            return _render(
                request,
                "mailbox.html",
                {"mapping": mapping, "emails": [], "error": f"CloudMail 查询失败：{exc}"},
                status_code=status.HTTP_502_BAD_GATEWAY,
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

    @app.post("/admin/cloudmail")
    def admin_save_cloudmail_settings(
        request: Request,
        base_url: str = Form(...),
        api_token: str = Form(...),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            request.app.state.store.save_cloudmail_settings(base_url=base_url, api_token=api_token)
        except ValueError as exc:
            return _render_admin_dashboard(request, error=_translate_store_error(str(exc)), status_code=status.HTTP_400_BAD_REQUEST)

        return _render_admin_dashboard(request, message="CloudMail 配置已保存")

    @app.post("/admin/keys")
    def admin_create_key(
        request: Request,
        recipient_email: str = Form(...),
        query_email: str = Form(""),
        access_key: str = Form(""),
        label: str = Form(""),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            request.app.state.store.create_mapping(
                recipient_email=recipient_email,
                query_email=query_email or None,
                access_key=access_key or None,
                label=label,
            )
        except ValueError as exc:
            return _render_admin_dashboard(request, error=_translate_store_error(str(exc)), status_code=status.HTTP_400_BAD_REQUEST)

        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/admin/keys/{mapping_id}/update")
    def admin_update_key(
        request: Request,
        mapping_id: int,
        recipient_email: str = Form(...),
        query_email: str = Form(""),
        access_key: str = Form(...),
        label: str = Form(""),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            request.app.state.store.update_mapping(
                mapping_id=mapping_id,
                recipient_email=recipient_email,
                query_email=query_email or None,
                access_key=access_key,
                label=label,
            )
        except ValueError as exc:
            return _render_admin_dashboard(request, error=_translate_store_error(str(exc)), status_code=status.HTTP_400_BAD_REQUEST)

        return _render_admin_dashboard(request, message="Key 已更新")

    @app.post("/admin/keys/{mapping_id}/delete")
    def admin_delete_key(request: Request, mapping_id: int) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            request.app.state.store.delete_mapping(mapping_id)
        except ValueError as exc:
            return _render_admin_dashboard(request, error=_translate_store_error(str(exc)), status_code=status.HTTP_400_BAD_REQUEST)

        return _render_admin_dashboard(request, message="Key 已删除")

    @app.post("/admin/logout")
    def admin_logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

    return app


def _render(request: Request, template_name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    merged = {"settings": request.app.state.settings, **context}
    return TEMPLATES.TemplateResponse(request, template_name, merged, status_code=status_code)


def _render_admin_dashboard(
    request: Request,
    error: str | None = None,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    mappings = request.app.state.store.list_mappings()
    cloudmail_config = _get_cloudmail_settings_for_display(request)
    return _render(
        request,
        "admin_dashboard.html",
        {
            "mappings": mappings,
            "cloudmail_config": cloudmail_config,
            "error": error,
            "message": message,
        },
        status_code=status_code,
    )


def _get_cloudmail_settings_for_display(request: Request) -> CloudMailSettingsRecord:
    settings = request.app.state.settings
    return request.app.state.store.get_cloudmail_settings(
        default_base_url=settings.cloudmail_base_url,
        default_api_token=settings.cloudmail_api_token or "",
    )


def _resolve_cloudmail_config(request: Request) -> ResolvedCloudMailConfig:
    settings = request.app.state.settings
    saved = _get_cloudmail_settings_for_display(request)
    return ResolvedCloudMailConfig(
        base_url=saved.base_url,
        api_token=saved.api_token or None,
        admin_email=settings.cloudmail_admin_email or None,
        admin_password=settings.cloudmail_admin_password or None,
    )


def _get_cloudmail_client(request: Request) -> Any:
    if request.app.state.fixed_cloudmail_client is not None:
        return request.app.state.fixed_cloudmail_client

    config = _resolve_cloudmail_config(request)
    if not config.base_url:
        raise CloudMailError("请先在后台填写 CloudMail 地址")
    if not config.api_token and not (config.admin_email and config.admin_password):
        raise CloudMailError("请先在后台填写 CloudMail Token")

    if request.app.state.cloudmail_client_factory is not None:
        return request.app.state.cloudmail_client_factory(config)

    return CloudMailClient(
        base_url=config.base_url,
        admin_email=config.admin_email,
        admin_password=config.admin_password,
        api_token=config.api_token,
    )


def _translate_store_error(message: str) -> str:
    mapping = {
        "recipient_email is required": "原始收件人邮箱不能为空",
        "query_email is required": "CloudMail 查询邮箱不能为空",
        "access_key is required": "查看 Key 不能为空",
        "access_key already exists": "这个查看 Key 已经存在",
        "mapping not found": "这个 Key 记录不存在或已被删除",
        "base_url is required": "CloudMail 地址不能为空",
        "api_token is required": "CloudMail Token 不能为空",
    }
    return mapping.get(message, message)


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
