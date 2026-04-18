from __future__ import annotations

import hmac
import html
import json
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
_HTML_LINE_BREAK_PATTERN = re.compile(r"<(?:br|/p|/div|/li|/tr|/td|/th|/h[1-6]|/section|/article|/header|/footer)\b[^>]*>", re.IGNORECASE)
_HTML_BLOCK_PATTERN = re.compile(r"<(?:p|div|li|tr|td|th|h[1-6]|section|article|header|footer)\b[^>]*>", re.IGNORECASE)
_HTML_STYLE_SCRIPT_PATTERN = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_SOURCE_HINTS = (
    "@font-face",
    ".externalclass",
    "font-family:",
    "border-collapse:",
    "mso-",
    "-webkit-text-size-adjust",
    "-ms-text-size-adjust",
    "<style",
    "</style",
    "<table",
    "<div",
)
_EMAIL_PATTERN = r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}"
_ORIGINAL_RECIPIENT_PATTERNS = [
    re.compile(
        rf"(?:收件人|收件邮箱|收件地址|Recipient|Original Recipient|Original-Recipient|Final-Recipient|Delivered-To|X-Original-To|To)\s*[:：]\s*[<\"']?({_EMAIL_PATTERN})",
        re.IGNORECASE,
    ),
]
ADMIN_PAGE_SIZE = 10
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@dataclass(slots=True)
class ResolvedCloudMailConfig:
    base_url: str
    api_token: str | None = None
    admin_email: str | None = None
    admin_password: str | None = None
    internal_admin_email: str | None = None
    internal_admin_password: str | None = None


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

        cloudmail_settings = _get_cloudmail_settings_for_display(request)

        try:
            emails = _get_cloudmail_client(request).fetch_recent_emails(
                mapping.query_email,
                limit=cloudmail_settings.recent_email_limit,
            )
        except CloudMailError as exc:
            return _render(
                request,
                "mailbox.html",
                {"mapping": mapping, "emails": [], "error": f"CloudMail 查询失败：{exc}"},
                status_code=status.HTTP_502_BAD_GATEWAY,
            )

        filtered_emails, notice = _filter_emails_for_mapping(mapping.recipient_email, mapping.query_email, emails)

        rendered_emails = [
            {
                "message": email,
                "codes": extract_verification_codes(email.subject, email.text, email.content),
                "preview": _build_preview(email.text, email.content),
                "detected_recipients": detected_recipients,
            }
            for email, detected_recipients in filtered_emails
        ]
        return _render(
            request,
            "mailbox.html",
            {"mapping": mapping, "emails": rendered_emails, "error": None, "notice": notice},
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
    def admin_dashboard(request: Request, q: str = "", page: int = 1) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        return _render_admin_dashboard(request, search_query=q, page=page)

    @app.post("/admin/cloudmail")
    def admin_save_cloudmail_settings(
        request: Request,
        base_url: str = Form(...),
        api_token: str = Form(""),
        internal_admin_email: str = Form(""),
        internal_admin_password: str = Form(""),
        default_query_email: str = Form(""),
        recent_email_limit: str = Form(...),
        q: str = Form(""),
        page: int = Form(1),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            request.app.state.store.save_cloudmail_settings(
                base_url=base_url,
                api_token=api_token,
                internal_admin_email=internal_admin_email,
                internal_admin_password=internal_admin_password,
                default_query_email=default_query_email,
                recent_email_limit=recent_email_limit,
            )
        except ValueError as exc:
            return _render_admin_dashboard(
                request,
                error=_translate_store_error(str(exc)),
                status_code=status.HTTP_400_BAD_REQUEST,
                search_query=q,
                page=page,
            )

        return _render_admin_dashboard(request, message="CloudMail 配置已保存", search_query=q, page=page)

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
                query_email=_resolve_query_email(request, query_email),
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
        q: str = Form(""),
        page: int = Form(1),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            request.app.state.store.update_mapping(
                mapping_id=mapping_id,
                recipient_email=recipient_email,
                query_email=_resolve_query_email(request, query_email),
                access_key=access_key,
                label=label,
            )
        except ValueError as exc:
            return _render_admin_dashboard(
                request,
                error=_translate_store_error(str(exc)),
                status_code=status.HTTP_400_BAD_REQUEST,
                search_query=q,
                page=page,
            )

        return _render_admin_dashboard(request, message="Key 已更新", search_query=q, page=page)

    @app.post("/admin/keys/{mapping_id}/delete")
    def admin_delete_key(request: Request, mapping_id: int, q: str = Form(""), page: int = Form(1)) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            request.app.state.store.delete_mapping(mapping_id)
        except ValueError as exc:
            return _render_admin_dashboard(
                request,
                error=_translate_store_error(str(exc)),
                status_code=status.HTTP_400_BAD_REQUEST,
                search_query=q,
                page=page,
            )

        return _render_admin_dashboard(request, message="Key 已删除", search_query=q, page=page)

    @app.post("/admin/keys/batch-delete")
    def admin_batch_delete_keys(
        request: Request,
        mapping_ids: list[int] = Form([]),
        q: str = Form(""),
        page: int = Form(1),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            deleted_count = request.app.state.store.delete_mappings(mapping_ids)
        except ValueError as exc:
            return _render_admin_dashboard(
                request,
                error=_translate_store_error(str(exc)),
                status_code=status.HTTP_400_BAD_REQUEST,
                search_query=q,
                page=page,
            )

        return _render_admin_dashboard(
            request,
            message=f"已批量删除 {deleted_count} 个 Key",
            search_query=q,
            page=page,
        )

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
    search_query: str = "",
    page: int = 1,
) -> HTMLResponse:
    normalized_page = max(page, 1)
    total_mappings = request.app.state.store.count_mappings(search_query=search_query)
    total_pages = max((total_mappings - 1) // ADMIN_PAGE_SIZE + 1, 1)
    current_page = min(normalized_page, total_pages)
    offset = (current_page - 1) * ADMIN_PAGE_SIZE
    mappings = request.app.state.store.list_mappings(
        search_query=search_query,
        limit=ADMIN_PAGE_SIZE,
        offset=offset,
    )
    cloudmail_config = _get_cloudmail_settings_for_display(request)
    return _render(
        request,
        "admin_dashboard.html",
        {
            "mappings": mappings,
            "cloudmail_config": cloudmail_config,
            "error": error,
            "message": message,
            "search_query": search_query,
            "current_page": current_page,
            "total_pages": total_pages,
            "total_mappings": total_mappings,
            "has_previous_page": current_page > 1,
            "has_next_page": current_page < total_pages,
            "previous_page": current_page - 1,
            "next_page": current_page + 1,
        },
        status_code=status_code,
    )


def _get_cloudmail_settings_for_display(request: Request) -> CloudMailSettingsRecord:
    settings = request.app.state.settings
    return request.app.state.store.get_cloudmail_settings(
        default_base_url=settings.cloudmail_base_url,
        default_api_token=settings.cloudmail_api_token or "",
        default_internal_admin_email=settings.cloudmail_internal_admin_email,
        default_internal_admin_password=settings.cloudmail_internal_admin_password,
        default_recent_email_limit=settings.lookup_email_limit,
    )


def _resolve_cloudmail_config(request: Request) -> ResolvedCloudMailConfig:
    settings = request.app.state.settings
    saved = _get_cloudmail_settings_for_display(request)
    return ResolvedCloudMailConfig(
        base_url=saved.base_url,
        api_token=saved.api_token or None,
        admin_email=settings.cloudmail_admin_email or None,
        admin_password=settings.cloudmail_admin_password or None,
        internal_admin_email=saved.internal_admin_email or None,
        internal_admin_password=saved.internal_admin_password or None,
    )


def _resolve_query_email(request: Request, query_email: str) -> str | None:
    normalized_query_email = query_email.strip().lower()
    if normalized_query_email:
        return normalized_query_email

    default_query_email = _get_cloudmail_settings_for_display(request).default_query_email.strip().lower()
    return default_query_email or None


def _get_cloudmail_client(request: Request) -> Any:
    if request.app.state.fixed_cloudmail_client is not None:
        return request.app.state.fixed_cloudmail_client

    config = _resolve_cloudmail_config(request)
    if not config.base_url:
        raise CloudMailError("请先在后台填写 CloudMail 地址")
    has_internal_login = bool(config.internal_admin_email and config.internal_admin_password)
    has_public_access = bool(config.api_token or (config.admin_email and config.admin_password))
    if not has_internal_login and not has_public_access:
        raise CloudMailError("请先填写固定 Token，或填写 CloudMail 管理员邮箱和密码")

    if request.app.state.cloudmail_client_factory is not None:
        return request.app.state.cloudmail_client_factory(config)

    return CloudMailClient(
        base_url=config.base_url,
        admin_email=config.admin_email,
        admin_password=config.admin_password,
        api_token=config.api_token,
        internal_admin_email=config.internal_admin_email,
        internal_admin_password=config.internal_admin_password,
    )


def _translate_store_error(message: str) -> str:
    mapping = {
        "recipient_email is required": "原始收件人邮箱不能为空",
        "query_email is required": "CloudMail 查询邮箱不能为空",
        "access_key is required": "查看 Key 不能为空",
        "access_key already exists": "这个查看 Key 已经存在",
        "mapping not found": "这个 Key 记录不存在或已被删除",
        "mapping_ids is required": "请至少选择一个 Key",
        "base_url is required": "CloudMail 地址不能为空",
        "api_token is required": "CloudMail Token 不能为空",
        "cloudmail_auth is required": "固定 Token 和管理员邮箱密码至少填一种",
        "internal_admin_credentials incomplete": "管理员邮箱和密码要么都填，要么都留空",
        "recent_email_limit must be a positive integer": "最新邮件数量必须是大于 0 的整数",
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
    normalized_text = _normalize_plain_preview(text_value)
    normalized_html = _normalize_html_preview(html_value)

    if normalized_text and not _looks_like_html_source(text_value):
        return normalized_text
    if normalized_html:
        return normalized_html
    return normalized_text


def _normalize_plain_preview(value: str) -> str:
    if not value:
        return ""

    normalized = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t\f\v]+", " ", normalized)
    normalized = re.sub(r"\n[ \t]+", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _normalize_html_preview(value: str) -> str:
    if not value:
        return ""

    normalized = html.unescape(value)
    normalized = _HTML_COMMENT_PATTERN.sub(" ", normalized)
    normalized = _HTML_STYLE_SCRIPT_PATTERN.sub(" ", normalized)
    normalized = _HTML_LINE_BREAK_PATTERN.sub("\n", normalized)
    normalized = _HTML_BLOCK_PATTERN.sub("\n", normalized)
    normalized = _TAG_PATTERN.sub(" ", normalized)
    normalized = normalized.replace("\xa0", " ")
    return _normalize_plain_preview(normalized)


def _looks_like_html_source(value: str) -> bool:
    if not value:
        return False

    normalized = html.unescape(value).lower()
    return any(marker in normalized for marker in _HTML_SOURCE_HINTS)


def _filter_emails_for_mapping(
    recipient_email: str,
    query_email: str,
    emails: list[Any],
) -> tuple[list[tuple[Any, list[str]]], str | None]:
    normalized_recipient_email = recipient_email.strip().lower()
    normalized_query_email = query_email.strip().lower()

    if normalized_recipient_email == normalized_query_email:
        return [(email, _extract_original_recipients(email)) for email in emails], None

    filtered: list[tuple[Any, list[str]]] = []
    detected_any = False

    for email in emails:
        detected_recipients = _extract_original_recipients(email)
        if detected_recipients:
            detected_any = True
        if normalized_recipient_email in detected_recipients:
            filtered.append((email, detected_recipients))

    if filtered:
        return filtered, None
    if not emails:
        return [], None
    if detected_any:
        return [], "当前 CloudMail 查询邮箱下有邮件，但识别到的原始收件人都不匹配这个 Key。"
    return [], "当前 CloudMail 查询邮箱下有邮件，但无法从 CloudMail 返回内容里识别原始收件人，所以没有直接展示共享查询邮箱里的混合邮件。"


def _extract_original_recipients(message: Any) -> list[str]:
    candidates: set[str] = set()

    raw_recipient = getattr(message, "recipient", "") or ""
    if raw_recipient:
        try:
            recipient_items = json.loads(raw_recipient)
        except json.JSONDecodeError:
            recipient_items = []
        for item in recipient_items:
            if isinstance(item, dict):
                address = str(item.get("address", "")).strip().lower()
                if address:
                    candidates.add(address)

    searchable_parts = [
        message.subject or "",
        message.text or "",
        _TAG_PATTERN.sub(" ", message.content or ""),
    ]

    for part in searchable_parts:
        normalized = _WHITESPACE_PATTERN.sub(" ", part)
        for pattern in _ORIGINAL_RECIPIENT_PATTERNS:
            for match in pattern.finditer(normalized):
                candidates.add(match.group(1).strip().lower())

    return sorted(candidates)


app = create_app()
