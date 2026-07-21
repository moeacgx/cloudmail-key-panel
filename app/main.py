from __future__ import annotations

import base64
import binascii
import hmac
import html
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app.cloudmail import CloudMailClient, CloudMailError
from app.code_extractor import extract_verification_codes
from app.mailbox_matching import PlatformRule, build_platform_rule, find_latest_code, max_email_id
from app.settings import AppSettings
from app.store import (
    AccessMapping,
    CloudMailSettingsRecord,
    KeyStore,
    MAPPING_STATUS_LABELS,
    TagOption,
    VerificationExtractionSettingsRecord,
)
from app.verification_extractor import (
    OpenAICompatibleCodeExtractor,
    VerificationCodeExtractor,
    VerificationExtractionError,
    openai_base_urls_share_origin,
    validate_openai_base_url,
)

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
MAILBOX_POLL_INTERVAL_MS = 10000
WORKBENCH_POLL_INTERVAL_MS = 5000
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


class ApiClaimRequest(BaseModel):
    category_id: int = Field(gt=0)
    target_tag_id: int = Field(gt=0)
    address_mode: str = "primary"


class ApiCompleteRequest(BaseModel):
    mapping_id: int = Field(gt=0)
    category_id: int = Field(gt=0)
    prevent_shared_pool: bool = False


class ApiSkipRequest(BaseModel):
    mapping_id: int = Field(gt=0)
    category_id: int = Field(gt=0)
    address_mode: str | None = None
    prevent_shared_pool: bool = False


def create_app(
    settings: AppSettings | None = None,
    store: KeyStore | None = None,
    cloudmail_client: Any | None = None,
    cloudmail_client_factory: Callable[[ResolvedCloudMailConfig], Any] | None = None,
    verification_ai_transport: Any | None = None,
) -> FastAPI:
    resolved_settings = settings or AppSettings.from_env()
    resolved_store = store or KeyStore(resolved_settings.database_path)

    app = FastAPI(title=resolved_settings.app_name)
    app.add_middleware(
        SessionMiddleware,
        secret_key=resolved_settings.app_secret_key,
        max_age=max(1, resolved_settings.redemption_session_hours) * 60 * 60,
        same_site="lax",
    )
    app.state.settings = resolved_settings
    app.state.store = resolved_store
    app.state.fixed_cloudmail_client = cloudmail_client
    app.state.cloudmail_client_factory = cloudmail_client_factory
    app.state.verification_ai_transport = verification_ai_transport

    @app.middleware("http")
    async def authenticate_external_api(request: Request, call_next: Callable[..., Any]) -> Response:
        if request.url.path.startswith("/api/v1/"):
            if error_response := _validate_external_api_request(request):
                return error_response
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        return _render(
            request,
            "index.html",
            {
                "error": None,
                "header_centered": True,
                "public_poll_interval_ms": WORKBENCH_POLL_INTERVAL_MS,
            },
        )

    @app.get("/key-lookup", response_class=HTMLResponse)
    def key_lookup(request: Request) -> Response:
        return _render(request, "key_lookup.html", {"error": None, "header_centered": True})

    @app.post("/lookup")
    def lookup(request: Request, access_key: str = Form(...)) -> Response:
        key = access_key.strip()
        if not key:
            return _render(
                request,
                "key_lookup.html",
                {"error": "请输入查看 Key", "header_centered": True},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return RedirectResponse(url=f"/mailbox/{key}", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/mailbox/{access_key}", response_class=HTMLResponse)
    def mailbox(request: Request, access_key: str) -> Response:
        mailbox_context, status_code = _build_mailbox_context(request, access_key)
        return _render(
            request,
            "mailbox.html",
            {
                **mailbox_context,
                "access_key": access_key,
                "mailbox_poll_interval_ms": MAILBOX_POLL_INTERVAL_MS,
            },
            status_code=status_code,
        )

    @app.get("/mailbox/{access_key}/fragment", response_class=HTMLResponse)
    def mailbox_fragment(request: Request, access_key: str) -> HTMLResponse:
        mailbox_context, status_code = _build_mailbox_context(request, access_key)
        return _render(request, "mailbox_content.html", {**mailbox_context, "access_key": access_key}, status_code=status_code)

    @app.get("/admin/login", response_class=HTMLResponse)
    def admin_login(request: Request) -> Response:
        return _render(request, "admin_login.html", {"error": None})

    @app.post("/admin/login")
    def admin_login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ) -> Response:
        if _is_valid_admin_login(request.app.state.settings, username, password):
            request.session.clear()
            request.session["is_admin"] = True
            request.session["workbench_session_id"] = secrets.token_urlsafe(16)
            return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

        return _render(
            request,
            "admin_login.html",
            {"error": "账号或密码错误"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    @app.get("/admin", response_class=HTMLResponse)
    def admin_dashboard(request: Request, q: str = "", category: str = "", page: int = 1) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        return _render_admin_dashboard(request, search_query=q, category_filter=category, page=page)

    @app.get("/admin/workbench", response_class=HTMLResponse)
    def admin_workbench(request: Request) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        tag_options = [
            tag
            for tag in request.app.state.store.list_tag_options()
            if tag.kind != "system"
        ]

        return _render(
            request,
            "admin_workbench.html",
            {
                "source_tags": tag_options,
                "platform_tags": [tag for tag in tag_options if tag.kind == "service"],
                # 旧模板变量仅用于本次升级期间的服务端渲染兜底。
                "categories": [tag.name for tag in tag_options],
                "workbench_poll_interval_ms": WORKBENCH_POLL_INTERVAL_MS,
            },
        )

    @app.get("/admin/api", response_class=HTMLResponse)
    def admin_api_docs(request: Request) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        response = _render(
            request,
            "admin_api.html",
            {
                "api_base_url": f"{str(request.base_url).rstrip('/')}/api/v1",
                "api_admin_username": request.app.state.settings.app_admin_username,
                "category_options": request.app.state.store.list_category_options(),
                "platform_tags": [
                    tag
                    for tag in request.app.state.store.list_tag_options()
                    if tag.kind == "service"
                ],
                "debug_client_id": "panel-debug",
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/categories")
    def api_v1_categories(request: Request) -> JSONResponse:
        if error_response := _validate_external_api_request(request):
            return error_response

        return _api_json(
            {
                "categories": [
                    {"id": category.id, "name": category.name, "count": category.count}
                    for category in request.app.state.store.list_category_options()
                ]
            }
        )

    @app.get("/api/v1/tags")
    def api_v1_tags(request: Request) -> JSONResponse:
        if error_response := _validate_external_api_request(request):
            return error_response
        return _api_json(
            {
                "tags": [
                    {
                        "id": tag.id,
                        "name": tag.name,
                        "kind": tag.kind,
                        "count": tag.count,
                        "success_count": getattr(tag, "success_count", 0),
                        "archived": tag.archived,
                        "prevent_reuse": tag.prevents_reuse,
                        "alias_use_limit": tag.alias_use_limit,
                    }
                    for tag in request.app.state.store.list_tag_options()
                    if tag.kind != "system"
                ]
            }
        )

    @app.post("/api/v1/workbench/claim-next")
    def api_v1_workbench_claim_next(request: Request, payload: ApiClaimRequest) -> JSONResponse:
        if error_response := _validate_external_api_request(request):
            return error_response

        claimed_by, client_error = _get_external_api_claimed_by(request)
        if client_error is not None:
            return client_error

        source_tag = _selectable_source_tag(request.app.state.store, payload.category_id)
        if source_tag is None:
            return _api_error("source_tag_not_found", "来源标签不存在或不可用", status.HTTP_404_NOT_FOUND)
        target_tag = _selectable_platform_tag(request.app.state.store, payload.target_tag_id)
        if target_tag is None:
            return _api_error("target_tag_not_found", "接码平台标签不存在或不可用", status.HTTP_404_NOT_FOUND)
        if payload.address_mode not in {"primary", "icloud_alias"}:
            return _api_error("address_mode_invalid", "邮箱方式无效", status.HTTP_400_BAD_REQUEST)

        current = request.app.state.store.get_current_workbench_mapping(claimed_by=claimed_by)
        if current is not None:
            if payload.category_id not in {
                tag.id for tag in request.app.state.store.list_mapping_tags(current.id)
            }:
                return _api_error(
                    "current_mapping_conflict",
                    "当前调用方已经领取了其他分类的邮箱，请先完成或跳过当前邮箱",
                    status.HTTP_409_CONFLICT,
                )
            if current.address_kind != payload.address_mode:
                return _api_error(
                    "current_address_mode_conflict",
                    "当前调用方已经领取了其他邮箱方式，请先完成或跳过当前邮箱",
                    status.HTTP_409_CONFLICT,
                )
            try:
                current = request.app.state.store.bind_workbench_target_tag(
                    current.id,
                    claimed_by=claimed_by,
                    target_tag_id=target_tag.id,
                )
            except ValueError:
                return _api_error(
                    "current_platform_conflict",
                    "当前调用方已经领取了其他平台的邮箱，请先完成或跳过当前邮箱",
                    status.HTTP_409_CONFLICT,
                )
            response_payload, response_status = _build_external_api_mailbox_payload(request, current)
            response_payload["message"] = "已恢复当前领取的邮箱"
            return _api_json(response_payload, status_code=response_status)

        mapping = request.app.state.store.claim_next_available_mapping(
            category_filter=source_tag.name,
            target_site=target_tag.name,
            claimed_by=claimed_by,
            address_mode=payload.address_mode,
            exclude_tag_id=target_tag.id,
            defer_email_baseline=True,
        )
        response_payload, response_status = _build_external_api_mailbox_payload(request, mapping)
        if mapping is None:
            response_payload["message"] = "当前分类下没有可领取邮箱"
        elif response_status == status.HTTP_200_OK:
            response_payload["message"] = "已领取下一个邮箱"
        else:
            response_payload["message"] = "邮箱已预留但尚未交付，请在邮件服务恢复后重试"
        return _api_json(response_payload, status_code=response_status)

    @app.get("/api/v1/workbench/current")
    def api_v1_workbench_current(request: Request) -> JSONResponse:
        if error_response := _validate_external_api_request(request):
            return error_response

        claimed_by, client_error = _get_external_api_claimed_by(request)
        if client_error is not None:
            return client_error

        mapping = request.app.state.store.get_current_workbench_mapping(claimed_by=claimed_by)
        response_payload, response_status = _build_external_api_mailbox_payload(request, mapping)
        if mapping is None:
            response_payload["message"] = "当前没有领取中的邮箱"
        elif response_status == status.HTTP_200_OK:
            response_payload["message"] = "已恢复当前领取的邮箱"
        else:
            response_payload["message"] = "当前邮箱尚未完成邮件快照，请稍后重试"
        return _api_json(response_payload, status_code=response_status)

    @app.post("/api/v1/workbench/complete")
    def api_v1_workbench_complete(request: Request, payload: ApiCompleteRequest) -> JSONResponse:
        if error_response := _validate_external_api_request(request):
            return error_response

        claimed_by, client_error = _get_external_api_claimed_by(request)
        if client_error is not None:
            return client_error

        source_tag = _selectable_source_tag(request.app.state.store, payload.category_id)
        if source_tag is None:
            return _api_error("source_tag_not_found", "来源标签不存在或不可用", status.HTTP_404_NOT_FOUND)

        current = request.app.state.store.get_current_workbench_mapping(claimed_by=claimed_by)
        if current is None:
            return _api_error("current_mapping_not_found", "当前没有领取中的邮箱", status.HTTP_404_NOT_FOUND)
        if current.id != payload.mapping_id:
            return _api_error("mapping_conflict", "mapping_id 不是当前领取的邮箱", status.HTTP_409_CONFLICT)
        if payload.category_id not in {
            tag.id for tag in request.app.state.store.list_mapping_tags(current.id)
        }:
            return _api_error("category_conflict", "category_id 与当前邮箱分类不一致", status.HTTP_409_CONFLICT)
        target_tag = _platform_tag_for_mapping(request.app.state.store, current)
        if target_tag is None:
            return _api_error("target_tag_not_found", "当前邮箱的接码平台标签已失效", status.HTTP_409_CONFLICT)
        mailbox_payload, mailbox_status = _build_external_api_mailbox_payload(request, current)
        if mailbox_status != status.HTTP_200_OK:
            extraction_error = mailbox_payload.get("error", {})
            return _api_error(
                extraction_error.get("code", "cloudmail_error"),
                extraction_error.get("message", "CloudMail 查询失败"),
                mailbox_status,
            )
        if not mailbox_payload.get("latest_code"):
            return _api_error(
                "verification_code_not_found",
                "尚未获取到验证码，不能标记为已使用",
                status.HTTP_409_CONFLICT,
            )
        latest_email = mailbox_payload.get("latest_email") or {}
        latest_email_id = int(latest_email.get("email_id") or 0)
        if latest_email_id <= 0:
            return _api_error(
                "verification_email_invalid",
                "验证码邮件缺少可记账的邮件编号",
                status.HTTP_409_CONFLICT,
            )
        try:
            completed = request.app.state.store.complete_workbench_mapping(
                mapping_id=current.id,
                target_tag_id=target_tag.id,
                claimed_by=claimed_by,
                verification_source="external_api",
                email_id=latest_email_id,
                prevent_reuse=payload.prevent_shared_pool,
            )
            next_mapping = request.app.state.store.claim_next_available_mapping(
                category_filter=source_tag.name,
                target_site=target_tag.name,
                claimed_by=claimed_by,
                address_mode="primary" if payload.prevent_shared_pool else current.address_kind,
                exclude_tag_id=target_tag.id,
                defer_email_baseline=True,
            )
        except ValueError as exc:
            return _api_error("workbench_error", _translate_store_error(str(exc)), status.HTTP_409_CONFLICT)

        response_payload, response_status = _build_external_api_mailbox_payload(request, next_mapping)
        response_payload["completed"] = _serialize_external_api_mapping(request, completed)
        if next_mapping is not None and response_payload.get("mapping") is None:
            response_payload["message"] = (
                f"已记录成功接码并追加“{target_tag.name}”标签；"
                "下一个邮箱尚未完成邮件快照，请调用 current 重试"
            )
        elif next_mapping is not None:
            response_payload["message"] = f"已记录成功接码并追加“{target_tag.name}”标签，同时领取下一个邮箱"
        else:
            response_payload["message"] = f"已记录成功接码并追加“{target_tag.name}”标签，暂无下一个可领取邮箱"
        # 成功事件和平台标签已经原子落库；即使下一条的邮件查询暂时失败，也返回成功，
        # 避免调用方把同一次完成操作误判为未执行而重复提交。
        return _api_json(response_payload)

    @app.post("/api/v1/workbench/skip-current")
    def api_v1_workbench_skip_current(request: Request, payload: ApiSkipRequest) -> JSONResponse:
        if error_response := _validate_external_api_request(request):
            return error_response

        claimed_by, client_error = _get_external_api_claimed_by(request)
        if client_error is not None:
            return client_error

        source_tag = _selectable_source_tag(request.app.state.store, payload.category_id)
        if source_tag is None:
            return _api_error("source_tag_not_found", "来源标签不存在或不可用", status.HTTP_404_NOT_FOUND)

        current = request.app.state.store.get_current_workbench_mapping(claimed_by=claimed_by)
        if current is None:
            return _api_error("current_mapping_not_found", "当前没有领取中的邮箱", status.HTTP_404_NOT_FOUND)
        if current.id != payload.mapping_id:
            return _api_error("mapping_conflict", "mapping_id 不是当前领取的邮箱", status.HTTP_409_CONFLICT)
        if payload.category_id not in {
            tag.id for tag in request.app.state.store.list_mapping_tags(current.id)
        }:
            return _api_error("category_conflict", "category_id 与当前邮箱分类不一致", status.HTTP_409_CONFLICT)
        if payload.address_mode is not None and payload.address_mode not in {"primary", "icloud_alias"}:
            return _api_error("address_mode_invalid", "邮箱方式无效", status.HTTP_400_BAD_REQUEST)

        target_tag = _platform_tag_for_mapping(request.app.state.store, current)
        if target_tag is None:
            return _api_error("target_tag_not_found", "当前邮箱的接码平台标签已失效", status.HTTP_409_CONFLICT)
        mailbox_payload, mailbox_status = _build_external_api_mailbox_payload(request, current)
        if mailbox_status != status.HTTP_200_OK:
            return _api_error(
                "cloudmail_error",
                mailbox_payload.get("error", {}).get("message", "CloudMail 查询失败"),
                mailbox_status,
            )

        latest_code = str(mailbox_payload.get("latest_code") or "").strip()
        try:
            completed = None
            released = None
            if latest_code:
                latest_email = mailbox_payload.get("latest_email") or {}
                latest_email_id = int(latest_email.get("email_id") or 0)
                if latest_email_id <= 0:
                    return _api_error(
                        "verification_email_invalid",
                        "验证码邮件缺少可记账的邮件编号",
                        status.HTTP_409_CONFLICT,
                    )
                completed = request.app.state.store.complete_workbench_mapping(
                    mapping_id=current.id,
                    target_tag_id=target_tag.id,
                    claimed_by=claimed_by,
                    verification_source="external_api",
                    email_id=latest_email_id,
                    prevent_reuse=payload.prevent_shared_pool,
                )
            else:
                released = request.app.state.store.reset_mapping_status(current.id, claimed_by=claimed_by)
            next_mapping = request.app.state.store.claim_next_available_mapping(
                category_filter=source_tag.name,
                target_site=target_tag.name,
                claimed_by=claimed_by,
                after_mapping_id=(
                    (current.parent_mapping_id or current.id)
                    if current.address_kind == "primary"
                    else None
                ),
                address_mode=payload.address_mode or current.address_kind,
                exclude_tag_id=target_tag.id,
                defer_email_baseline=True,
            )
        except ValueError as exc:
            return _api_error("workbench_error", _translate_store_error(str(exc)), status.HTTP_409_CONFLICT)
        response_payload, response_status = _build_external_api_mailbox_payload(request, next_mapping)
        response_payload["skipped"] = _serialize_external_api_mapping(request, released)
        response_payload["completed"] = _serialize_external_api_mapping(request, completed)
        next_delivered = next_mapping is not None and response_payload.get("mapping") is not None
        next_pending_snapshot = next_mapping is not None and not next_delivered
        if next_pending_snapshot:
            response_payload["message"] = (
                "当前邮箱已经处理；下一个邮箱尚未完成邮件快照，请调用 current 重试"
            )
        elif latest_code:
            response_payload["message"] = (
                "验证码已经到达，已按成功接码记录并领取下一个"
                if next_delivered
                else "验证码已经到达，已按成功接码记录；暂无下一个可领取邮箱"
            )
        else:
            response_payload["message"] = (
                "已跳过当前邮箱并领取下一个"
                if next_delivered
                else "当前邮箱已释放，暂无下一个可领取邮箱"
            )
        # 跳过和下一条领取已经改变状态，邮件查询错误通过响应体返回，不把状态操作伪装成失败。
        return _api_json(response_payload)

    @app.get("/api/workbench/current")
    def api_workbench_current(request: Request, category: str = "") -> JSONResponse:
        if not _is_admin(request):
            return _json_error("unauthorized", status.HTTP_401_UNAUTHORIZED)

        mapping = request.app.state.store.get_current_workbench_mapping(
            claimed_by=_get_workbench_session_id(request),
        )
        if mapping is not None:
            try:
                mapping = _ensure_workbench_claim_baseline(request, mapping)
            except CloudMailError as exc:
                return _json_error(
                    f"邮箱尚未交付，邮件快照初始化失败：{exc}",
                    status.HTTP_502_BAD_GATEWAY,
                )
        return JSONResponse(
            {
                "mapping": _serialize_workbench_mapping(request, mapping),
                "message": (
                    "已恢复当前邮箱，请先选择本次接码平台"
                    if mapping is not None and not mapping.target_site.strip()
                    else ("已恢复当前注册中的邮箱" if mapping else "当前没有注册中的邮箱")
                ),
            }
        )

    @app.post("/api/workbench/claim-next")
    def api_workbench_claim_next(
        request: Request,
        category: str = Form(""),
        target_tag_id: int = Form(...),
        address_mode: str = Form("primary"),
        prevent_shared_pool: bool = Form(False),
    ) -> JSONResponse:
        if not _is_admin(request):
            return _json_error("unauthorized", status.HTTP_401_UNAUTHORIZED)
        if address_mode not in {"primary", "icloud_alias"}:
            return _json_error("邮箱方式无效", status.HTTP_400_BAD_REQUEST)
        target_tag = _selectable_platform_tag(request.app.state.store, target_tag_id)
        if target_tag is None:
            return _json_error("请选择有效的平台标签", status.HTTP_400_BAD_REQUEST)

        workbench_session_id = _get_workbench_session_id(request)
        current = request.app.state.store.get_current_workbench_mapping(claimed_by=workbench_session_id)
        after_mapping_id = None
        completed_by_arrived_code = False
        if current is not None:
            if not current.target_site.strip():
                try:
                    current = request.app.state.store.bind_workbench_target_tag(
                        current.id,
                        claimed_by=workbench_session_id,
                        target_tag_id=target_tag.id,
                    )
                except ValueError as exc:
                    return _json_error(
                        _translate_store_error(str(exc)),
                        status.HTTP_409_CONFLICT,
                    )
            current_target_tag = _platform_tag_for_mapping(request.app.state.store, current)
            if current_target_tag is None or current_target_tag.id != target_tag.id:
                return _json_error(
                    "当前邮箱属于其他接码平台，请先刷新工作台后再操作",
                    status.HTTP_409_CONFLICT,
                )
            if not request.app.state.store.is_workbench_claim_baseline_ready(
                current.id,
                claimed_by=workbench_session_id,
            ):
                try:
                    current = _ensure_workbench_claim_baseline(request, current)
                except CloudMailError as exc:
                    return _json_error(
                        f"邮箱已预留但尚未交付，邮件快照初始化失败：{exc}",
                        status.HTTP_502_BAD_GATEWAY,
                    )
                return JSONResponse(
                    {
                        "mapping": _serialize_workbench_mapping(request, current),
                        "message": "已恢复此前预留的邮箱",
                    }
                )
            mailbox_payload, mailbox_status = _build_workbench_latest_code_payload(request, current)
            if mailbox_status != status.HTTP_200_OK:
                return _json_error(
                    mailbox_payload.get("error") or "验证码查询失败，请稍后重试",
                    mailbox_status,
                )
            if mailbox_payload.get("latest_code"):
                try:
                    request.app.state.store.complete_workbench_mapping(
                        mapping_id=current.id,
                        target_tag_id=current_target_tag.id,
                        claimed_by=workbench_session_id,
                        verification_source="admin_workbench",
                        email_id=int(mailbox_payload.get("latest_email_id") or 0),
                        prevent_reuse=prevent_shared_pool,
                    )
                except ValueError as exc:
                    return _json_error(_translate_store_error(str(exc)), status.HTTP_409_CONFLICT)
                completed_by_arrived_code = True
            else:
                request.app.state.store.reset_mapping_status(current.id, claimed_by=workbench_session_id)
            selected_source_tag_id = request.app.state.store.get_category_id(category)
            current_source_tag_ids = {
                tag.id for tag in request.app.state.store.list_mapping_tags(current.id)
            }
            if current.address_kind == "primary" and (
                not category.strip() or selected_source_tag_id in current_source_tag_ids
            ):
                after_mapping_id = current.parent_mapping_id or current.id

        mapping = request.app.state.store.claim_next_available_mapping(
            category_filter=category,
            target_site=target_tag.name,
            claimed_by=workbench_session_id,
            after_mapping_id=after_mapping_id,
            address_mode="primary" if prevent_shared_pool else address_mode,
            exclude_tag_id=target_tag.id,
            defer_email_baseline=True,
        )
        if mapping is not None:
            try:
                mapping = _ensure_workbench_claim_baseline(request, mapping)
            except CloudMailError as exc:
                return _json_error(
                    f"邮箱已预留但尚未交付，邮件快照初始化失败：{exc}",
                    status.HTTP_502_BAD_GATEWAY,
                )
        if current is not None:
            if completed_by_arrived_code:
                message = (
                    "验证码已经到达，已按成功接码记录并领取下一个"
                    if mapping
                    else "验证码已经到达，已按成功接码记录；暂无下一个可领取邮箱"
                )
            else:
                message = "已跳过当前邮箱并领取下一个" if mapping else "当前邮箱已释放，暂无下一个可领取邮箱"
        else:
            message = "已领取下一个邮箱" if mapping else "当前分类下没有可领取邮箱"
        return JSONResponse(
            {
                "mapping": _serialize_workbench_mapping(request, mapping),
                "message": message,
            }
        )

    @app.get("/api/workbench/current/mailbox")
    def api_workbench_current_mailbox(
        request: Request,
        category: str = "",
        target_tag_id: int | None = None,
    ) -> JSONResponse:
        if not _is_admin(request):
            return _json_error("unauthorized", status.HTTP_401_UNAUTHORIZED)

        mapping = request.app.state.store.get_current_workbench_mapping(
            claimed_by=_get_workbench_session_id(request),
        )
        if mapping is None:
            return JSONResponse(
                {
                    "mapping": None,
                    "latest_code": None,
                    "error": None,
                    "notice": "当前没有注册中的邮箱",
                }
            )

        if not mapping.target_site.strip() and target_tag_id is None:
            return _json_error("请先选择本次接码平台后再刷新验证码", status.HTTP_409_CONFLICT)

        if target_tag_id is not None:
            target_tag = _selectable_platform_tag(request.app.state.store, target_tag_id)
            if target_tag is None:
                return _json_error("请选择有效的平台标签", status.HTTP_400_BAD_REQUEST)
            if not mapping.target_site.strip():
                try:
                    mapping = request.app.state.store.bind_workbench_target_tag(
                        mapping.id,
                        claimed_by=_get_workbench_session_id(request),
                        target_tag_id=target_tag.id,
                    )
                except ValueError as exc:
                    return _json_error(
                        _translate_store_error(str(exc)),
                        status.HTTP_409_CONFLICT,
                    )
            current_target_tag = _platform_tag_for_mapping(request.app.state.store, mapping)
            if current_target_tag is None or current_target_tag.id != target_tag.id:
                return _json_error("当前邮箱的接码平台不一致", status.HTTP_409_CONFLICT)

        payload, status_code = _build_workbench_latest_code_payload(request, mapping)
        return JSONResponse(payload, status_code=status_code)

    @app.post("/api/workbench/current/mark-used")
    def api_workbench_mark_used(
        request: Request,
        mapping_id: int = Form(...),
        category: str = Form(""),
        target_tag_id: int = Form(...),
        address_mode: str = Form("primary"),
        prevent_shared_pool: bool = Form(False),
    ) -> JSONResponse:
        if not _is_admin(request):
            return _json_error("unauthorized", status.HTTP_401_UNAUTHORIZED)

        target_tag = _selectable_platform_tag(request.app.state.store, target_tag_id)
        if target_tag is None:
            return _json_error("请选择有效的平台标签", status.HTTP_400_BAD_REQUEST)

        current_mapping = request.app.state.store.get_by_id(mapping_id)
        latest_email_id = 0
        if current_mapping is not None:
            if not current_mapping.target_site.strip():
                try:
                    current_mapping = request.app.state.store.bind_workbench_target_tag(
                        current_mapping.id,
                        claimed_by=_get_workbench_session_id(request),
                        target_tag_id=target_tag.id,
                    )
                except ValueError as exc:
                    return _json_error(
                        _translate_store_error(str(exc)),
                        status.HTTP_409_CONFLICT,
                    )
            current_target_tag = _platform_tag_for_mapping(request.app.state.store, current_mapping)
            if current_target_tag is None or current_target_tag.id != target_tag.id:
                return _json_error("当前邮箱的接码平台不一致", status.HTTP_409_CONFLICT)
            mailbox_payload, mailbox_status = _build_workbench_latest_code_payload(request, current_mapping)
            if mailbox_status != status.HTTP_200_OK:
                return _json_error(
                    mailbox_payload.get("error") or "验证码查询失败，请稍后重试",
                    mailbox_status,
                )
            if not mailbox_payload.get("latest_code"):
                return _json_error("尚未获取到验证码，不能标记为已使用", status.HTTP_409_CONFLICT)
            latest_email_id = int(mailbox_payload.get("latest_email_id") or 0)
            if latest_email_id <= 0:
                return _json_error("验证码邮件缺少可记账的邮件编号", status.HTTP_409_CONFLICT)

        completed, next_mapping, message, status_code = _complete_workbench_mapping(
            request,
            mapping_id=mapping_id,
            target_tag_id=target_tag.id,
            category=category,
            claimed_by=_get_workbench_session_id(request),
            address_mode=address_mode,
            prevent_reuse=prevent_shared_pool,
            email_id=latest_email_id,
            verification_source="admin_workbench",
        )
        return JSONResponse(
            {
                "completed": _serialize_workbench_mapping(request, completed),
                "mapping": _serialize_workbench_mapping(request, next_mapping),
                "message": message,
            },
            status_code=status_code,
        )

    @app.post("/api/workbench/current/skip")
    def api_workbench_skip(
        request: Request,
        mapping_id: int = Form(...),
        category: str = Form(""),
        target_tag_id: int = Form(...),
        prevent_shared_pool: bool = Form(False),
    ) -> JSONResponse:
        if not _is_admin(request):
            return _json_error("unauthorized", status.HTTP_401_UNAUTHORIZED)

        mapping = request.app.state.store.get_by_id(mapping_id)
        if mapping is None:
            return _json_error("这个 Key 记录不存在或已被删除", status.HTTP_404_NOT_FOUND)
        if mapping.status != "in_progress":
            return _json_error("只能取消注册中的邮箱", status.HTTP_400_BAD_REQUEST)
        if mapping.claimed_by != _get_workbench_session_id(request):
            return _json_error("这个邮箱不是当前工作台领取的", status.HTTP_409_CONFLICT)
        target_tag = _selectable_platform_tag(request.app.state.store, target_tag_id)
        if target_tag is not None and not mapping.target_site.strip():
            try:
                mapping = request.app.state.store.bind_workbench_target_tag(
                    mapping.id,
                    claimed_by=_get_workbench_session_id(request),
                    target_tag_id=target_tag.id,
                )
            except ValueError as exc:
                return _json_error(
                    _translate_store_error(str(exc)),
                    status.HTTP_409_CONFLICT,
                )
        current_target_tag = _platform_tag_for_mapping(request.app.state.store, mapping)
        if target_tag is None or current_target_tag is None or target_tag.id != current_target_tag.id:
            return _json_error("当前邮箱的接码平台不一致", status.HTTP_409_CONFLICT)

        mailbox_payload, mailbox_status = _build_workbench_latest_code_payload(request, mapping)
        if mailbox_status != status.HTTP_200_OK:
            return _json_error(
                mailbox_payload.get("error") or "验证码查询失败，请稍后重试",
                mailbox_status,
            )

        try:
            received_code = bool(mailbox_payload.get("latest_code"))
            if received_code:
                latest_email_id = int(mailbox_payload.get("latest_email_id") or 0)
                if latest_email_id <= 0:
                    return _json_error("验证码邮件缺少可记账的邮件编号", status.HTTP_409_CONFLICT)
                completed = request.app.state.store.complete_workbench_mapping(
                    mapping_id=mapping_id,
                    target_tag_id=target_tag.id,
                    claimed_by=_get_workbench_session_id(request),
                    verification_source="admin_workbench",
                    email_id=latest_email_id,
                    prevent_reuse=prevent_shared_pool,
                )
            else:
                completed = request.app.state.store.reset_mapping_status(
                    mapping_id,
                    claimed_by=_get_workbench_session_id(request),
                )
        except ValueError as exc:
            return _json_error(_translate_store_error(str(exc)), status.HTTP_409_CONFLICT)
        return JSONResponse(
            {
                "completed": _serialize_workbench_mapping(request, completed),
                "mapping": None,
                "message": (
                    "验证码已经到达，已按成功接码记录并追加标签"
                    if received_code
                    else "已取消领取，标签未改变"
                ),
            }
        )

    @app.post("/api/workbench/current/reset-status")
    def api_workbench_reset_status(
        request: Request,
        mapping_id: int = Form(...),
    ) -> JSONResponse:
        if not _is_admin(request):
            return _json_error("unauthorized", status.HTTP_401_UNAUTHORIZED)

        mapping = request.app.state.store.get_by_id(mapping_id)
        if mapping is None:
            return _json_error("这个 Key 记录不存在或已被删除", status.HTTP_404_NOT_FOUND)
        if mapping.status != "in_progress":
            return _json_error("只能重置注册中的当前邮箱", status.HTTP_400_BAD_REQUEST)
        if mapping.claimed_by != _get_workbench_session_id(request):
            return _json_error("这个邮箱不是当前工作台领取的", status.HTTP_409_CONFLICT)

        try:
            reset_mapping = request.app.state.store.reset_mapping_status(
                mapping_id,
                claimed_by=_get_workbench_session_id(request),
            )
        except ValueError as exc:
            return _json_error(_translate_store_error(str(exc)), status.HTTP_409_CONFLICT)
        return JSONResponse(
            {
                "completed": _serialize_workbench_mapping(request, reset_mapping),
                "mapping": None,
                "message": "已取消领取，标签未改变",
            }
        )

    @app.post("/api/admin/keys/{mapping_id}/reset-status")
    def api_admin_reset_key_status(request: Request, mapping_id: int) -> JSONResponse:
        if not _is_admin(request):
            return _json_error("unauthorized", status.HTTP_401_UNAUTHORIZED)

        mapping = request.app.state.store.get_by_id(mapping_id)
        if mapping is None:
            return _json_error("Key record does not exist or was deleted", status.HTTP_404_NOT_FOUND)

        try:
            reset_mapping = request.app.state.store.reset_mapping_status(mapping_id)
        except ValueError as exc:
            return _json_error(_translate_store_error(str(exc)), status.HTTP_400_BAD_REQUEST)

        return JSONResponse(
            {
                "mapping": _serialize_workbench_mapping(request, reset_mapping),
                "message": "Workbench claim cancelled",
            }
        )

    @app.post("/admin/cloudmail")
    def admin_save_cloudmail_settings(
        request: Request,
        base_url: str = Form(...),
        api_token: str = Form(""),
        internal_admin_email: str = Form(""),
        internal_admin_password: str = Form(""),
        default_query_email: str = Form(""),
        recent_email_limit: str = Form(...),
        display_timezone: str = Form("UTC"),
        q: str = Form(""),
        category: str = Form(""),
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
                display_timezone=display_timezone,
            )
        except ValueError as exc:
            return _render_admin_dashboard(
                request,
                error=_translate_store_error(str(exc)),
                status_code=status.HTTP_400_BAD_REQUEST,
                search_query=q,
                category_filter=category,
                page=page,
            )

        return _render_admin_dashboard(
            request,
            message="CloudMail 配置已保存",
            search_query=q,
            category_filter=category,
            page=page,
        )

    @app.post("/admin/verification-extraction")
    def admin_save_verification_extraction_settings(
        request: Request,
        mode: str = Form("off"),
        custom_patterns: str = Form(""),
        base_url: str = Form(""),
        api_key: str = Form(""),
        model: str = Form(""),
        timeout_seconds: str = Form("10"),
        clear_api_key: bool = Form(False),
        q: str = Form(""),
        category: str = Form(""),
        page: int = Form(1),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        try:
            request.app.state.store.save_verification_extraction_settings(
                mode=mode,
                custom_patterns=custom_patterns,
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
                clear_api_key=clear_api_key,
            )
        except ValueError as exc:
            return _render_admin_dashboard(
                request,
                error=_translate_store_error(str(exc)),
                status_code=status.HTTP_400_BAD_REQUEST,
                search_query=q,
                category_filter=category,
                page=page,
            )
        return _render_admin_dashboard(
            request,
            message="验证码提取配置已保存",
            search_query=q,
            category_filter=category,
            page=page,
        )

    @app.post("/admin/verification-extraction/test")
    def admin_test_verification_extraction_settings(
        request: Request,
        base_url: str = Form(""),
        api_key: str = Form(""),
        model: str = Form(""),
        timeout_seconds: str = Form("10"),
        clear_api_key: bool = Form(False),
    ) -> JSONResponse:
        if not _is_admin(request):
            return _json_error("登录已失效，请重新登录", status.HTTP_401_UNAUTHORIZED)
        if clear_api_key:
            return _json_error("已勾选清除密钥，无法测试 AI 接口", status.HTTP_400_BAD_REQUEST)

        saved = _get_verification_settings(request)
        try:
            normalized_base_url = validate_openai_base_url(base_url)
            submitted_api_key = api_key.strip()
            if submitted_api_key:
                normalized_api_key = submitted_api_key
            elif openai_base_urls_share_origin(normalized_base_url, saved.base_url):
                normalized_api_key = saved.api_key
            elif saved.api_key:
                raise ValueError("verification ai api key is required for changed origin")
            else:
                normalized_api_key = ""
            normalized_model = model.strip()
            normalized_timeout = int(timeout_seconds)
            if not 1 <= normalized_timeout <= 60:
                raise ValueError("verification ai timeout is invalid")
            if not normalized_base_url or not normalized_api_key or not normalized_model:
                raise ValueError("verification ai config is incomplete")

            code = OpenAICompatibleCodeExtractor(
                base_url=normalized_base_url,
                api_key=normalized_api_key,
                model=normalized_model,
                timeout_seconds=normalized_timeout,
                transport=getattr(request.app.state, "verification_ai_transport", None),
            ).extract(
                subject="CloudMail interface test",
                text="Your verification code is TEST-7Q9.",
                html_content="",
            )
            if code != "TEST-7Q9":
                raise VerificationExtractionError("AI 接口可访问，但未正确返回测试验证码")
        except ValueError as exc:
            return _json_error(
                _translate_store_error(str(exc)),
                status.HTTP_400_BAD_REQUEST,
            )
        except VerificationExtractionError as exc:
            message = str(exc)
            if "HTTP 401" in message:
                message = "AI 接口拒绝了当前密钥（HTTP 401），请重新填写该接口对应的 API Key"
            elif "HTTP 403" in message:
                message = "AI 接口拒绝访问（HTTP 403），请检查密钥权限和模型权限"
            elif "HTTP 404" in message:
                message = "AI 接口路径不存在（HTTP 404），通常应填写到 /v1"
            elif message == "AI 接口返回的不是有效 JSON":
                message = "AI 接口返回的不是 JSON，请确认填写的是 /v1 接口地址而不是网站首页"
            return _json_error(message, status.HTTP_424_FAILED_DEPENDENCY)

        return _api_json(
            {
                "ok": True,
                "message": f"接口测试成功，模型 {normalized_model} 已正确返回 TEST-7Q9",
            }
        )

    @app.post("/admin/keys")
    def admin_create_key(
        request: Request,
        recipient_email: str = Form(...),
        query_email: str = Form(""),
        access_key: str = Form(""),
        label: str = Form(""),
        category: str = Form(""),
        category_custom: str = Form(""),
        tag_ids: list[int] = Form([]),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            recipient_emails = _parse_recipient_emails(recipient_email)
            if len(recipient_emails) > 1 and access_key.strip():
                raise ValueError("批量导入多个邮箱时不能自定义单个 Key，请留空自动生成")

            resolved_query_email = _resolve_query_email(request, query_email)
            resolved_category = _resolve_mapping_category(category, category_custom)
            if not resolved_category:
                resolved_category = _legacy_category_from_tag_ids(request.app.state.store, tag_ids)
            for index, email in enumerate(recipient_emails):
                created_mapping = request.app.state.store.create_mapping(
                    recipient_email=email,
                    query_email=resolved_query_email,
                    access_key=access_key if index == 0 and len(recipient_emails) == 1 else None,
                    label=label,
                    category=resolved_category,
                )
                resolved_tag_ids = list(tag_ids)
                category_id = request.app.state.store.get_category_id(resolved_category)
                if category_id is not None and category_id not in resolved_tag_ids:
                    resolved_tag_ids.append(category_id)
                request.app.state.store.set_mapping_tags(created_mapping.id, resolved_tag_ids)
        except ValueError as exc:
            return _render_admin_dashboard(
                request,
                error=_translate_store_error(str(exc)),
                status_code=status.HTTP_400_BAD_REQUEST,
                create_key_form_open=True,
            )

        message = "Key 已创建" if len(recipient_emails) == 1 else f"已批量创建 {len(recipient_emails)} 个 Key"
        return _render_admin_dashboard(request, message=message)

    @app.post("/admin/keys/{mapping_id}/update")
    def admin_update_key(
        request: Request,
        mapping_id: int,
        recipient_email: str = Form(...),
        query_email: str = Form(""),
        access_key: str = Form(...),
        label: str = Form(""),
        category_value: str = Form(""),
        category_custom: str = Form(""),
        tag_ids: list[int] = Form([]),
        q: str = Form(""),
        category: str = Form(""),
        page: int = Form(1),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            resolved_category = _resolve_mapping_category(category_value, category_custom)
            if not resolved_category:
                resolved_category = _legacy_category_from_tag_ids(request.app.state.store, tag_ids)
            updated_mapping = request.app.state.store.update_mapping(
                mapping_id=mapping_id,
                recipient_email=recipient_email,
                query_email=_resolve_query_email(request, query_email),
                access_key=access_key,
                label=label,
                category=resolved_category,
            )
            resolved_tag_ids = list(tag_ids)
            category_id = request.app.state.store.get_category_id(resolved_category)
            if category_id is not None and category_id not in resolved_tag_ids:
                resolved_tag_ids.append(category_id)
            request.app.state.store.set_mapping_tags(updated_mapping.id, resolved_tag_ids)
        except ValueError as exc:
            return _render_admin_dashboard(
                request,
                error=_translate_store_error(str(exc)),
                status_code=status.HTTP_400_BAD_REQUEST,
                search_query=q,
                category_filter=category,
                page=page,
            )

        return _render_admin_dashboard(
            request,
            message="Key 已更新",
            search_query=q,
            category_filter=category,
            page=page,
        )

    @app.post("/admin/keys/{mapping_id}/delete")
    def admin_delete_key(
        request: Request,
        mapping_id: int,
        q: str = Form(""),
        category: str = Form(""),
        page: int = Form(1),
    ) -> Response:
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
                category_filter=category,
                page=page,
            )

        return _render_admin_dashboard(
            request,
            message="Key 已删除",
            search_query=q,
            category_filter=category,
            page=page,
        )

    @app.post("/admin/keys/{mapping_id}/reset-status")
    def admin_reset_key_status(
        request: Request,
        mapping_id: int,
        q: str = Form(""),
        category: str = Form(""),
        page: int = Form(1),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

        try:
            request.app.state.store.reset_mapping_status(mapping_id)
        except ValueError as exc:
            return _render_admin_dashboard(
                request,
                error=_translate_store_error(str(exc)),
                status_code=status.HTTP_400_BAD_REQUEST,
                search_query=q,
                category_filter=category,
                page=page,
            )

        return _render_admin_dashboard(
            request,
            message="工作台占用已取消",
            search_query=q,
            category_filter=category,
            page=page,
        )

    @app.post("/admin/keys/batch-delete")
    def admin_batch_delete_keys(
        request: Request,
        mapping_ids: list[int] = Form([]),
        q: str = Form(""),
        category: str = Form(""),
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
                category_filter=category,
                page=page,
            )

        return _render_admin_dashboard(
            request,
            message=f"已批量删除 {deleted_count} 个 Key",
            search_query=q,
            category_filter=category,
            page=page,
        )

    @app.post("/admin/logout")
    def admin_logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)

    from app.registration_routes import register_registration_routes

    register_registration_routes(
        app,
        render=_render,
        get_cloudmail_client=_get_cloudmail_client,
        get_cloudmail_settings=_get_cloudmail_settings_for_display,
        get_verification_extractor=_get_verification_code_extractor,
    )

    return app


def _render(request: Request, template_name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    merged = {"settings": request.app.state.settings, **context}
    return TEMPLATES.TemplateResponse(request, template_name, merged, status_code=status_code)


def _json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message, "message": message}, status_code=status_code)


def _api_json(payload: dict[str, Any], status_code: int = status.HTTP_200_OK) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _api_error(
    code: str,
    message: str,
    status_code: int,
    *,
    authenticate: bool = False,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if authenticate:
        headers["WWW-Authenticate"] = 'Basic realm="CloudMail Key Panel API", charset="UTF-8"'
    return JSONResponse(
        {"error": {"code": code, "message": message}, "message": message},
        status_code=status_code,
        headers=headers,
    )


def _validate_external_api_request(request: Request) -> JSONResponse | None:
    authorization = request.headers.get("Authorization", "").strip()
    scheme, separator, credentials = authorization.partition(" ")
    if not separator or scheme.casefold() != "basic" or not credentials.strip():
        return _api_error(
            "unauthorized",
            "需要使用后台账号进行 HTTP Basic 认证",
            status.HTTP_401_UNAUTHORIZED,
            authenticate=True,
        )

    try:
        decoded = base64.b64decode(credentials.strip(), validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return _api_error(
            "unauthorized",
            "HTTP Basic 认证信息无效",
            status.HTTP_401_UNAUTHORIZED,
            authenticate=True,
        )

    if not _is_valid_admin_login(request.app.state.settings, username, password):
        return _api_error(
            "unauthorized",
            "后台账号或密码错误",
            status.HTTP_401_UNAUTHORIZED,
            authenticate=True,
        )
    return None


def _get_external_api_claimed_by(request: Request) -> tuple[str, JSONResponse | None]:
    client_id = request.headers.get("X-Client-ID", "").strip()
    if not client_id:
        return "", _api_error(
            "client_id_required",
            "缺少 X-Client-ID 请求头",
            status.HTTP_400_BAD_REQUEST,
        )
    if len(client_id) > 128 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", client_id) is None:
        return "", _api_error(
            "client_id_invalid",
            "X-Client-ID 只能包含字母、数字、点、下划线、冒号和短横线，且最长 128 个字符",
            status.HTTP_400_BAD_REQUEST,
        )
    return f"api:{client_id}", None


def _build_mailbox_context(request: Request, access_key: str) -> tuple[dict[str, Any], int]:
    mapping = request.app.state.store.get_by_key(access_key)
    if mapping is None:
        return {"mapping": None, "emails": [], "error": "Key 不存在或已失效", "notice": None}, status.HTTP_404_NOT_FOUND

    cloudmail_settings = _get_cloudmail_settings_for_display(request)

    try:
        emails = _get_cloudmail_client(request).fetch_recent_emails(
            mapping.query_email,
            limit=cloudmail_settings.recent_email_limit,
        )
    except CloudMailError as exc:
        return {
            "mapping": mapping,
            "emails": [],
            "error": f"CloudMail 查询失败：{exc}",
            "notice": None,
        }, status.HTTP_502_BAD_GATEWAY

    filtered_emails, notice = _filter_emails_for_mapping(mapping.recipient_email, mapping.query_email, emails)
    display_timezone = cloudmail_settings.display_timezone
    rendered_emails = [
        {
            "message": email,
            "codes": extract_verification_codes(email.subject, email.text, email.content),
            "preview": _build_preview(email.text, email.content),
            "detected_recipients": detected_recipients,
            "display_create_time": _format_timestamp_for_display(email.create_time, display_timezone),
        }
        for email, detected_recipients in filtered_emails
    ]
    return {
        "mapping": mapping,
        "emails": rendered_emails,
        "error": None,
        "notice": notice,
        "display_timezone": display_timezone,
    }, status.HTTP_200_OK


def _ensure_workbench_claim_baseline(request: Request, mapping: AccessMapping) -> AccessMapping:
    """在邮箱交付前建立一次持久化快照；已完成的快照不会被重复推进。"""

    store: KeyStore = request.app.state.store
    if store.is_workbench_claim_baseline_ready(mapping.id, claimed_by=mapping.claimed_by):
        return mapping

    cloudmail_settings = _get_cloudmail_settings_for_display(request)
    emails = _get_cloudmail_client(request).fetch_recent_emails(
        mapping.query_email,
        limit=cloudmail_settings.recent_email_limit,
    )
    baseline = max_email_id(emails, baseline=mapping.last_seen_email_id)
    return store.finalize_workbench_claim_baseline(
        mapping.id,
        claimed_by=mapping.claimed_by,
        baseline_email_id=baseline,
    )


def _build_workbench_latest_code_payload(request: Request, mapping: AccessMapping) -> tuple[dict[str, Any], int]:
    cloudmail_settings = _get_cloudmail_settings_for_display(request)
    baseline_was_ready = request.app.state.store.is_workbench_claim_baseline_ready(
        mapping.id,
        claimed_by=mapping.claimed_by,
    )

    try:
        mapping = _ensure_workbench_claim_baseline(request, mapping)
        if not baseline_was_ready:
            # 第一次成功查询只负责划定“邮箱交付前”的邮件边界。若继续在同一
            # 请求里再次查码，快照与响应之间到达的邮件会在用户尚未拿到邮箱
            # 时被误算为本次验证码。
            return {
                "mapping": _serialize_workbench_mapping(request, mapping),
                "latest_code": None,
                "latest_email_id": None,
                "recipient_match": None,
                "error": None,
                "notice": "邮箱快照已建立，请开始注册并等待新验证码。",
            }, status.HTTP_200_OK
        emails = _get_cloudmail_client(request).fetch_recent_emails(
            mapping.query_email,
            limit=cloudmail_settings.recent_email_limit,
        )
    except CloudMailError as exc:
        return {
            "mapping": None,
            "latest_code": None,
            "latest_email_id": None,
            "recipient_match": None,
            "error": f"CloudMail 查询失败：{exc}",
            "notice": None,
        }, status.HTTP_502_BAD_GATEWAY

    fallback_email = _alias_fallback_email(request.app.state.store, mapping)
    try:
        matched = find_latest_code(
            emails,
            actual_email=mapping.recipient_email,
            claimed_at=mapping.claimed_at,
            baseline_email_id=mapping.last_seen_email_id,
            platform_rule=_platform_rule_for_mapping(request, mapping),
            fallback_email=fallback_email,
            allow_recipient_fallback=bool(fallback_email),
            code_extractor=_get_verification_code_extractor(request),
        )
    except VerificationExtractionError as exc:
        return {
            "mapping": _serialize_workbench_mapping(request, mapping),
            "latest_code": None,
            "latest_email_id": None,
            "recipient_match": None,
            "error": str(exc),
            "notice": None,
        }, status.HTTP_502_BAD_GATEWAY

    return {
        "mapping": _serialize_workbench_mapping(request, mapping),
        "latest_code": None if matched is None else matched.code,
        "latest_email_id": None if matched is None else matched.email_id,
        "recipient_match": (
            None if matched is None else ("root_fallback" if matched.matched_via_fallback else "exact")
        ),
        "error": None,
        "notice": None if matched is not None or not emails else "暂未收到本次领取后且符合平台规则的新验证码。",
    }, status.HTTP_200_OK


def _build_external_api_mailbox_payload(
    request: Request,
    mapping: AccessMapping | None,
) -> tuple[dict[str, Any], int]:
    if mapping is None:
        return {
            "mapping": None,
            "registration_email": None,
            "latest_code": None,
            "latest_email": None,
            "recipient_match": None,
            "notice": None,
            "error": None,
        }, status.HTTP_200_OK

    cloudmail_settings = _get_cloudmail_settings_for_display(request)
    baseline_was_ready = request.app.state.store.is_workbench_claim_baseline_ready(
        mapping.id,
        claimed_by=mapping.claimed_by,
    )
    try:
        mapping = _ensure_workbench_claim_baseline(request, mapping)
    except CloudMailError as exc:
        return {
            "mapping": None,
            "registration_email": None,
            "latest_code": None,
            "latest_email": None,
            "recipient_match": None,
            "notice": "邮箱尚未交付；邮件快照初始化成功后才会开始接码。",
            "error": {"code": "cloudmail_error", "message": f"CloudMail 查询失败：{exc}"},
        }, status.HTTP_502_BAD_GATEWAY
    if not baseline_was_ready:
        # 新领取的第一次 CloudMail 查询是交付快照，不是验证码轮询。调用方
        # 拿到邮箱后，再从下一次 current/complete/skip 请求开始查新邮件。
        return {
            "mapping": _serialize_external_api_mapping(request, mapping),
            "registration_email": mapping.recipient_email,
            "latest_code": None,
            "latest_email": None,
            "recipient_match": None,
            "notice": "邮箱快照已建立，请开始注册并等待新验证码。",
            "error": None,
        }, status.HTTP_200_OK
    try:
        emails = _get_cloudmail_client(request).fetch_recent_emails(
            mapping.query_email,
            limit=cloudmail_settings.recent_email_limit,
        )
    except CloudMailError as exc:
        return {
            "mapping": _serialize_external_api_mapping(request, mapping),
            "registration_email": mapping.recipient_email,
            "latest_code": None,
            "latest_email": None,
            "recipient_match": None,
            "notice": None,
            "error": {"code": "cloudmail_error", "message": f"CloudMail 查询失败：{exc}"},
        }, status.HTTP_502_BAD_GATEWAY

    fallback_email = _alias_fallback_email(request.app.state.store, mapping)
    try:
        matched = find_latest_code(
            emails,
            actual_email=mapping.recipient_email,
            claimed_at=mapping.claimed_at,
            baseline_email_id=mapping.last_seen_email_id,
            platform_rule=_platform_rule_for_mapping(request, mapping),
            fallback_email=fallback_email,
            allow_recipient_fallback=bool(fallback_email),
            code_extractor=_get_verification_code_extractor(request),
        )
    except VerificationExtractionError as exc:
        return {
            "mapping": _serialize_external_api_mapping(request, mapping),
            "registration_email": mapping.recipient_email,
            "latest_code": None,
            "latest_email": None,
            "recipient_match": None,
            "notice": None,
            "error": {"code": "verification_extraction_error", "message": str(exc)},
        }, status.HTTP_502_BAD_GATEWAY
    latest_email = None
    if matched is not None:
        email = next(
            (message for message in emails if int(getattr(message, "email_id", 0) or 0) == matched.email_id),
            None,
        )
        if email is not None:
            detected_recipients = [
                recipient
                for recipient in matched.recipients
                if recipient != mapping.query_email.casefold()
                or mapping.recipient_email.casefold() == mapping.query_email.casefold()
            ]
            latest_email = {
                "email_id": email.email_id,
                "send_email": email.send_email,
                "send_name": email.send_name,
                "subject": email.subject,
                "to_email": email.to_email or mapping.recipient_email,
                "to_name": email.to_name,
                "create_time": email.create_time,
                "display_create_time": _format_timestamp_for_display(
                    email.create_time,
                    cloudmail_settings.display_timezone,
                ),
                "type": email.type,
                "is_del": email.is_del,
                "recipient": email.recipient,
                "content": email.content,
                "text": email.text,
                "codes": [matched.code],
                "detected_recipients": detected_recipients,
            }

    return {
        "mapping": _serialize_external_api_mapping(request, mapping),
        "registration_email": mapping.recipient_email,
        "latest_code": None if matched is None else matched.code,
        "latest_email": latest_email,
        "recipient_match": (
            None if matched is None else ("root_fallback" if matched.matched_via_fallback else "exact")
        ),
        "notice": None if matched is not None or not emails else "暂未收到本次领取后且符合平台规则的新验证码。",
        "error": None,
    }, status.HTTP_200_OK


def _complete_workbench_mapping(
    request: Request,
    mapping_id: int,
    target_tag_id: int,
    category: str,
    claimed_by: str,
    address_mode: str = "primary",
    prevent_reuse: bool = False,
    email_id: int = 0,
    verification_source: str = "admin_workbench",
) -> tuple[AccessMapping | None, AccessMapping | None, str, int]:
    mapping = request.app.state.store.get_by_id(mapping_id)
    if mapping is None:
        return None, None, "这个 Key 记录不存在或已被删除", status.HTTP_404_NOT_FOUND
    if mapping.status != "in_progress":
        return None, None, "只能处理注册中的邮箱", status.HTTP_400_BAD_REQUEST
    if mapping.claimed_by != claimed_by:
        return None, None, "这个邮箱不是当前工作台领取的", status.HTTP_409_CONFLICT
    target_tag = _selectable_platform_tag(request.app.state.store, target_tag_id)
    current_target_tag = _platform_tag_for_mapping(request.app.state.store, mapping)
    if target_tag is None or current_target_tag is None or target_tag.id != current_target_tag.id:
        return None, None, "当前邮箱的接码平台不一致", status.HTTP_409_CONFLICT
    if int(email_id) <= 0:
        return None, None, "验证码邮件缺少可记账的邮件编号", status.HTTP_409_CONFLICT

    completed = request.app.state.store.complete_workbench_mapping(
        mapping_id=mapping_id,
        target_tag_id=target_tag.id,
        claimed_by=claimed_by,
        verification_source=verification_source,
        email_id=int(email_id),
        prevent_reuse=prevent_reuse,
    )
    next_mapping = request.app.state.store.claim_next_available_mapping(
        category_filter=category,
        target_site=target_tag.name,
        claimed_by=claimed_by,
        address_mode="primary" if prevent_reuse else address_mode,
        exclude_tag_id=target_tag.id,
        defer_email_baseline=True,
    )
    if next_mapping is None:
        return completed, None, f"已记录成功接码并追加“{target_tag.name}”标签，暂无下一个可领取邮箱", status.HTTP_200_OK
    try:
        next_mapping = _ensure_workbench_claim_baseline(request, next_mapping)
    except CloudMailError:
        return (
            completed,
            None,
            f"已记录成功接码并追加“{target_tag.name}”标签；下一个邮箱尚未完成快照，请重新领取",
            status.HTTP_200_OK,
        )
    return completed, next_mapping, f"已记录成功接码并追加“{target_tag.name}”标签，同时领取下一个邮箱", status.HTTP_200_OK


def _resolve_mapping_category(category: str, custom_category: str) -> str:
    normalized_category = (category or "").strip()
    if normalized_category != "__custom__":
        return normalized_category

    normalized_custom = (custom_category or "").strip()
    if not normalized_custom:
        raise ValueError("category is required")
    return normalized_custom


def _legacy_category_from_tag_ids(store: KeyStore, tag_ids: list[int]) -> str:
    """为旧版单分类字段选择一个稳定值，界面只暴露多标签。"""

    normalized_ids = tuple(dict.fromkeys(int(tag_id) for tag_id in tag_ids if int(tag_id) > 0))
    if not normalized_ids:
        return ""

    selected_tags: list[TagOption] = []
    for tag_id in normalized_ids:
        tag = store.get_tag(tag_id)
        if tag is None or tag.archived or tag.kind == "system":
            raise ValueError("tag not found")
        selected_tags.append(tag)

    # 旧字段优先记录业务标签；真正的筛选和历史均以 mapping_tags 为准。
    preferred = next((tag for tag in selected_tags if tag.kind == "business"), selected_tags[0])
    return preferred.name


def _serialize_workbench_mapping(request: Request, mapping: AccessMapping | None) -> dict[str, Any] | None:
    if mapping is None:
        return None

    return {
        "id": mapping.id,
        "recipient_email": mapping.recipient_email,
        "query_email": mapping.query_email,
        "access_key": mapping.access_key,
        "label": mapping.label,
        "category": request.app.state.store.canonicalize_category(mapping.category),
        "created_at": mapping.created_at,
        "status": mapping.status,
        "status_label": MAPPING_STATUS_LABELS.get(mapping.status, mapping.status),
        "claimed_at": mapping.claimed_at,
        "used_at": mapping.used_at,
        "last_seen_email_id": mapping.last_seen_email_id,
        "target_site": mapping.target_site,
        "target_tag_id": request.app.state.store.get_category_id(mapping.target_site),
        "address_kind": mapping.address_kind,
        "parent_mapping_id": mapping.parent_mapping_id,
        "reuse_policy": mapping.reuse_policy,
        "first_used_at": mapping.first_used_at,
        "tags": list(mapping.tags),
        "mailbox_url": str(request.url_for("mailbox", access_key=mapping.access_key)),
    }


def _serialize_external_api_mapping(request: Request, mapping: AccessMapping | None) -> dict[str, Any] | None:
    if mapping is None:
        return None

    store: KeyStore = request.app.state.store
    source_tag = store.get_tag(mapping.claim_source_tag_id) if mapping.claim_source_tag_id > 0 else None
    if source_tag is None:
        category = store.canonicalize_category(mapping.category)
        category_id = store.get_category_id(category)
    else:
        category = source_tag.name
        category_id = source_tag.id
    tags = store.list_mapping_tags(mapping.id)
    return {
        "id": mapping.id,
        "registration_email": mapping.recipient_email,
        "label": mapping.label,
        "category_id": category_id,
        "category": category,
        "tags": [
            {
                "id": tag.id,
                "name": tag.name,
                "kind": tag.kind,
                "prevent_reuse": tag.prevents_reuse,
                "alias_use_limit": tag.alias_use_limit,
            }
            for tag in tags
        ],
        "created_at": mapping.created_at,
        "status": mapping.status,
        "claimed_at": mapping.claimed_at,
        "target_site": mapping.target_site,
        "target_tag_id": store.get_category_id(mapping.target_site),
        "address_mode": mapping.address_kind,
        "is_alias": mapping.address_kind == "icloud_alias",
    }


def _render_admin_dashboard(
    request: Request,
    error: str | None = None,
    message: str | None = None,
    status_code: int = 200,
    search_query: str = "",
    category_filter: str = "",
    page: int = 1,
    create_key_form_open: bool = False,
) -> HTMLResponse:
    normalized_page = max(page, 1)
    total_mappings = request.app.state.store.count_mappings(
        search_query=search_query,
        category_filter=category_filter,
        include_aliases=False,
    )
    total_pages = max((total_mappings - 1) // ADMIN_PAGE_SIZE + 1, 1)
    current_page = min(normalized_page, total_pages)
    offset = (current_page - 1) * ADMIN_PAGE_SIZE
    mappings = request.app.state.store.list_mappings(
        search_query=search_query,
        category_filter=category_filter,
        limit=ADMIN_PAGE_SIZE,
        offset=offset,
        include_aliases=False,
    )
    tags = [
        tag
        for tag in request.app.state.store.list_tag_options()
        if tag.kind != "system"
    ]
    categories = [tag.name for tag in tags]
    cloudmail_config = _get_cloudmail_settings_for_display(request)
    verification_config = _get_verification_settings(request)
    display_mappings = []
    for mapping in mappings:
        mapping_tags = request.app.state.store.list_mapping_tags(mapping.id)
        usage_state = (
            "successful"
            if mapping.first_used_at
            else "platform_tagged"
            if any(tag.kind == "service" for tag in mapping_tags)
            else "never_used"
        )
        display_mappings.append(
            {
                "id": mapping.id,
                "recipient_email": mapping.recipient_email,
                "query_email": mapping.query_email,
                "access_key": mapping.access_key,
                "label": mapping.label,
                "category": request.app.state.store.canonicalize_category(mapping.category),
                "created_at": _format_timestamp_for_display(
                    mapping.created_at,
                    cloudmail_config.display_timezone,
                ),
                "status": mapping.status,
                "status_label": MAPPING_STATUS_LABELS.get(mapping.status, mapping.status),
                "claimed_at": _format_timestamp_for_display(
                    mapping.claimed_at,
                    cloudmail_config.display_timezone,
                ),
                "used_at": _format_timestamp_for_display(
                    mapping.used_at,
                    cloudmail_config.display_timezone,
                ),
                "target_site": mapping.target_site,
                "tags": list(mapping.tags),
                "tag_ids": [tag.id for tag in mapping_tags],
                "address_kind": mapping.address_kind,
                "parent_mapping_id": mapping.parent_mapping_id,
                "reuse_policy": mapping.reuse_policy,
                "first_used_at": _format_timestamp_for_display(
                    mapping.first_used_at,
                    cloudmail_config.display_timezone,
                ),
                "usage_state": usage_state,
            }
        )
    return _render(
        request,
        "admin_dashboard.html",
        {
            "mappings": display_mappings,
            "categories": categories,
            "tags": tags,
            "cloudmail_config": cloudmail_config,
            "verification_config": verification_config,
            "error": error,
            "message": message,
            "search_query": search_query,
            "current_category": category_filter,
            "current_page": current_page,
            "total_pages": total_pages,
            "total_mappings": total_mappings,
            "has_previous_page": current_page > 1,
            "has_next_page": current_page < total_pages,
            "previous_page": current_page - 1,
            "next_page": current_page + 1,
            "create_key_form_open": create_key_form_open,
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
        default_display_timezone=settings.display_timezone,
    )


def _get_verification_settings(request: Request) -> VerificationExtractionSettingsRecord:
    settings = request.app.state.settings
    return request.app.state.store.get_verification_extraction_settings(
        default_mode=settings.verification_extraction_mode,
        default_custom_patterns=settings.verification_code_patterns,
        default_base_url=settings.verification_ai_base_url,
        default_api_key=settings.verification_ai_api_key,
        default_model=settings.verification_ai_model,
        default_timeout_seconds=settings.verification_ai_timeout_seconds,
    )


def _get_verification_code_extractor(
    request: Request,
) -> Callable[[str, str, str, PlatformRule], list[str]]:
    settings = _get_verification_settings(request)
    ai_extractor = None
    if settings.base_url and settings.api_key and settings.model:
        ai_extractor = OpenAICompatibleCodeExtractor(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            timeout_seconds=settings.timeout_seconds,
            transport=getattr(request.app.state, "verification_ai_transport", None),
        )
    ai_attempts_remaining = 3

    def extract(subject: str, text: str, html_content: str, rule: PlatformRule) -> list[str]:
        nonlocal ai_attempts_remaining
        if settings.mode == "only" or rule.extraction_mode == "ai_only":
            effective_mode = "ai_only"
        elif settings.mode == "fallback" or rule.extraction_mode == "ai_fallback":
            effective_mode = "ai_fallback"
        else:
            effective_mode = "rules"
        custom_patterns = tuple(
            dict.fromkeys((*settings.custom_patterns, *rule.code_patterns))
        )

        if effective_mode != "ai_only":
            rule_codes = VerificationCodeExtractor(
                mode="rules",
                custom_patterns=custom_patterns,
            ).extract(subject, text, html_content)
            if rule_codes or effective_mode == "rules":
                return rule_codes
        if ai_attempts_remaining <= 0:
            raise VerificationExtractionError("单次轮询的 AI 提取次数已达上限")
        ai_attempts_remaining -= 1
        return VerificationCodeExtractor(
            mode="ai_only",
            custom_patterns=custom_patterns,
            ai_extractor=ai_extractor,
        ).extract(subject, text, html_content)

    return extract


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


def _parse_recipient_emails(raw_value: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    for line in (raw_value or "").splitlines():
        normalized = line.strip().lower()
        if not normalized:
            continue
        if not re.fullmatch(_EMAIL_PATTERN, normalized, re.IGNORECASE):
            raise ValueError(f"原始收件人邮箱格式不正确：{line.strip()}")
        if normalized not in seen:
            candidates.append(normalized)
            seen.add(normalized)

    if not candidates:
        raise ValueError("recipient_email is required")

    return candidates


def _format_timestamp_for_display(value: str, timezone_name: str) -> str:
    try:
        parsed = datetime.strptime((value or "").strip(), "%Y-%m-%d %H:%M:%S")
        parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, ZoneInfoNotFoundError):
        return value


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
        "mapping has registration history": "该邮箱已有注册台领取记录，为保留接码历史暂不能删除",
        "mapping_ids is required": "请至少选择一个 Key",
        "base_url is required": "CloudMail 地址不能为空",
        "api_token is required": "CloudMail Token 不能为空",
        "cloudmail_auth is required": "固定 Token 和管理员邮箱密码至少填一种",
        "internal_admin_credentials incomplete": "管理员邮箱和密码要么都填，要么都留空",
        "recent_email_limit must be a positive integer": "最新邮件数量必须是大于 0 的整数",
        "display_timezone is invalid": "系统时区无效，请填写正确的 IANA 时区，例如 Asia/Shanghai",
        "verification ai base url is invalid": "AI 接口地址无效，请填写 http 或 https 地址",
        "verification ai config is incomplete": "AI 接口地址、密钥和模型必须同时填写",
        "verification ai config is required": "AI 兜底或仅 AI 模式必须完整配置接口地址、密钥和模型",
        "verification ai api key is required for changed origin": "接口域名已改变，请重新填写这个接口对应的 API Key",
        "verification ai timeout is invalid": "AI 请求超时必须是 1 到 60 秒的整数",
        "verification extraction global mode is invalid": "全局验证码提取模式无效",
        "verification extraction mode is invalid": "验证码提取模式无效",
        "verification code pattern is too long": "单条验证码正则最长 500 个字符",
        "verification code pattern is invalid": "验证码正则格式无效，请检查后重试",
        "too many verification code patterns": "验证码正则最多配置 20 条",
        "status is invalid": "邮箱状态无效",
        "category is required": "完成后分类不能为空",
        "claimed_by is required": "工作台会话无效，请刷新后重新登录",
        "claimed_by already has active mapping": "当前调用方已经领取了一个邮箱",
        "mapping not claimed by this session": "这个邮箱不是当前工作台领取的",
        "verification code completion tag is required": "验证码已经到达，请先选择成功后追加的标签，不能直接跳过",
        "verification code completion tag is invalid": "验证码已经到达，但成功标签不存在、已停用或不可用于平台记录",
    }
    return mapping.get(message, message)


def _selectable_source_tag(store: KeyStore, tag_id: int) -> TagOption | None:
    tag = store.get_tag(tag_id)
    if tag is None or tag.archived or tag.kind == "system":
        return None
    return tag


def _selectable_platform_tag(store: KeyStore, tag_id: int) -> TagOption | None:
    tag = _selectable_source_tag(store, tag_id)
    if tag is None or tag.kind != "service":
        return None
    return tag


def _platform_tag_for_mapping(store: KeyStore, mapping: AccessMapping) -> TagOption | None:
    target_tag_id = store.get_category_id(mapping.target_site)
    if target_tag_id is None:
        return None
    return _selectable_platform_tag(store, target_tag_id)


def _alias_fallback_email(store: KeyStore, mapping: AccessMapping) -> str:
    """工作台只有一条活动领取，因此裂变地址可安全回退到其主邮箱。"""

    if mapping.address_kind != "icloud_alias" or mapping.status != "in_progress":
        return ""
    root_mapping_id = int(mapping.parent_mapping_id or 0)
    if root_mapping_id <= 0:
        return ""
    root = store.get_by_id(root_mapping_id)
    return "" if root is None else root.recipient_email


def _is_valid_admin_login(settings: AppSettings, username: str, password: str) -> bool:
    username_matches = hmac.compare_digest(
        username.encode("utf-8"),
        settings.app_admin_username.encode("utf-8"),
    )
    password_matches = hmac.compare_digest(
        password.encode("utf-8"),
        settings.app_admin_password.encode("utf-8"),
    )
    return username_matches and password_matches


def _platform_rule_for_mapping(request: Request, mapping: AccessMapping):
    target_name = (mapping.target_site or "").strip()
    if not target_name:
        return build_platform_rule("", unrestricted=True)
    target_tag_id = request.app.state.store.get_category_id(target_name)
    tag = request.app.state.store.get_tag(target_tag_id) if target_tag_id is not None else None
    if tag is None:
        return build_platform_rule(target_name)
    return build_platform_rule(
        tag.name,
        sender_patterns=tag.sender_patterns,
        subject_keywords=tag.subject_keywords,
        code_patterns=tag.code_patterns,
        extraction_mode=tag.extraction_mode,
        unrestricted=tag.kind == "system",
    )


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _get_workbench_session_id(request: Request) -> str:
    session_id = str(request.session.get("workbench_session_id") or "").strip()
    if not session_id:
        session_id = secrets.token_urlsafe(16)
        request.session["workbench_session_id"] = session_id
    return session_id


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
        direct_recipient = str(getattr(email, "to_email", "") or "").strip().lower()
        if direct_recipient and direct_recipient != normalized_query_email:
            detected_recipients = sorted({*detected_recipients, direct_recipient})
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
