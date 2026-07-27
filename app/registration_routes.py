from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.cloudmail import CloudMailError
from app.mailbox_matching import CodeMatch, PlatformRule, build_platform_rule, find_latest_code, max_email_id
from app.store import CardBatch, KeyStore, RedemptionCard, RegistrationClaim, TagOption
from app.verification_extractor import VerificationExtractionError


class PublicRedeemRequest(BaseModel):
    card_code: str = Field(min_length=1, max_length=128)


class PublicClaimRequest(BaseModel):
    address_mode: str | None = Field(default=None, max_length=32)


def register_registration_routes(
    app: FastAPI,
    *,
    render: Callable[[Request, str, dict[str, Any], int], HTMLResponse],
    get_cloudmail_client: Callable[[Request], Any],
    get_cloudmail_settings: Callable[[Request], Any],
    get_verification_extractor: Callable[[Request], Any],
) -> None:
    """挂载兑换卡公开注册台及其后台管理路由。"""

    @app.post("/api/public/redeem")
    def public_redeem(request: Request, payload: PublicRedeemRequest) -> JSONResponse:
        store: KeyStore = request.app.state.store
        card = store.get_card_by_code(payload.card_code)
        if card is None:
            return _public_error("兑换卡不存在，请检查后重试。", status.HTTP_404_NOT_FOUND)
        unavailable = _card_unavailable_error(card)
        if unavailable is not None:
            message, status_code = unavailable
            return _public_error(message, status_code)
        request.session["redemption_card_code"] = card.code
        response_payload = _serialize_card_session(store, card)
        response_payload["message"] = "兑换卡验证成功。"
        return _public_json(response_payload)

    @app.get("/api/public/session")
    def public_session(request: Request) -> JSONResponse:
        store: KeyStore = request.app.state.store
        store.expire_registration_claims(request.app.state.settings.redemption_claim_minutes)
        card = _get_session_card(request)
        unavailable = _card_unavailable_error(card) if card is not None else None
        if card is None or unavailable is not None:
            request.session.pop("redemption_card_code", None)
            return _public_json(
                {
                    "valid": False,
                    "message": unavailable[0] if unavailable is not None else "当前没有兑换卡会话。",
                }
            )
        payload = _serialize_card_session(store, card)
        payload["message"] = "已恢复兑换卡会话。"
        return _public_json(payload)

    @app.delete("/api/public/session")
    def public_session_delete(request: Request) -> JSONResponse:
        request.session.pop("redemption_card_code", None)
        return _public_json({"valid": False, "message": "已退出当前兑换卡。"})

    @app.post("/api/public/claims")
    def public_create_claim(request: Request, payload: PublicClaimRequest) -> JSONResponse:
        store: KeyStore = request.app.state.store
        card = _get_session_card(request)
        if card is None:
            return _public_error("请先验证兑换卡。", status.HTTP_401_UNAUTHORIZED)
        try:
            claim = store.start_registration_claim(
                card.code,
                address_mode=payload.address_mode,
                timeout_minutes=request.app.state.settings.redemption_claim_minutes,
                defer_email_baseline=True,
            )
        except ValueError as exc:
            message, status_code = _translate_public_store_error(str(exc))
            refreshed_card = store.get_redemption_card(card.id) or card
            return _public_error(
                message,
                status_code,
                cooldown_until=_utc_iso(refreshed_card.cooldown_until),
                remaining_uses=refreshed_card.remaining_uses,
            )
        if not claim.baseline_ready:
            try:
                claim = _prime_claim_email_baseline(
                    request,
                    claim,
                    get_cloudmail_client=get_cloudmail_client,
                    get_cloudmail_settings=get_cloudmail_settings,
                )
            except (CloudMailError, VerificationExtractionError) as exc:
                refreshed_card = store.get_redemption_card(card.id) or card
                return _public_error(
                    f"邮件服务暂时不可用，邮箱尚未交付，请稍后重试：{exc}",
                    status.HTTP_502_BAD_GATEWAY,
                    baseline_ready=False,
                    remaining_uses=refreshed_card.remaining_uses,
                    cooldown_until=_utc_iso(refreshed_card.cooldown_until),
                )
        refreshed_card = store.get_redemption_card(card.id) or card
        return _public_json(
            {
                "claim": _serialize_claim(store, claim),
                "remaining_uses": refreshed_card.remaining_uses,
                "cooldown_until": _utc_iso(refreshed_card.cooldown_until),
                "baseline_ready": claim.baseline_ready,
                "message": "邮箱已生成，只有成功收到验证码后才会扣除次数。",
            },
            status_code=status.HTTP_201_CREATED,
        )

    @app.get("/api/public/claims/{claim_id}/code")
    def public_claim_code(request: Request, claim_id: int) -> JSONResponse:
        store: KeyStore = request.app.state.store
        claim = _authorize_claim(request, claim_id)
        if claim is None:
            return _public_error("邮箱记录不存在或查看凭证已失效。", status.HTTP_404_NOT_FOUND)
        if claim.revoked_at:
            return _public_error("这个邮箱记录已被管理员撤销。", status.HTTP_410_GONE)
        if claim.status in {"skipped", "timed_out"}:
            return _public_error("这个邮箱领取已结束，无法继续接码。", status.HTTP_410_GONE)

        matched: CodeMatch | None = None
        if not claim.superseded_at:
            try:
                matched = _find_claim_code(
                    request,
                    claim,
                    get_cloudmail_client=get_cloudmail_client,
                    get_cloudmail_settings=get_cloudmail_settings,
                    get_verification_extractor=get_verification_extractor,
                )
            except (CloudMailError, VerificationExtractionError) as exc:
                return _public_error(f"邮件查询暂时失败：{exc}", status.HTTP_502_BAD_GATEWAY)

        if matched is not None and claim.status == "pending":
            card = store.get_redemption_card(claim.card_id)
            if card is None:
                return _public_error("兑换卡记录不存在。", status.HTTP_410_GONE)
            try:
                claim = store.complete_registration_claim(
                    claim.id,
                    card_code=card.code,
                    verification_code=matched.code,
                    email_id=matched.email_id,
                )
            except ValueError as exc:
                message, status_code = _translate_public_store_error(str(exc))
                return _public_error(message, status_code)
        elif matched is not None and claim.status == "completed":
            try:
                claim = store.record_registration_claim_code(
                    claim.id,
                    verification_code=matched.code,
                    email_id=matched.email_id,
                )
            except ValueError as exc:
                message, status_code = _translate_public_store_error(str(exc))
                return _public_error(message, status_code)

        latest_code = matched.code if matched is not None else claim.verification_code
        latest_code_at = matched.create_time if matched is not None else claim.completed_at
        card = store.get_redemption_card(claim.card_id)
        return _public_json(
            {
                "claim": _serialize_claim(
                    store,
                    claim,
                    latest_code=latest_code,
                    latest_code_at=latest_code_at,
                ),
                "latest_code": latest_code,
                "latest_code_at": _utc_iso(latest_code_at),
                "remaining_uses": card.remaining_uses if card is not None else 0,
                "recipient_match": (
                    None
                    if matched is None
                    else ("root_fallback" if matched.matched_via_fallback else "exact")
                ),
                "message": (
                    "该邮箱族已有更新的领取地址，当前记录已冻结，只展示最后一次验证码。"
                    if claim.superseded_at
                    else ("已获取最新验证码。" if latest_code else "暂未收到符合条件的新验证码。")
                ),
            }
        )

    @app.post("/api/public/claims/{claim_id}/skip")
    def public_skip_claim(request: Request, claim_id: int) -> JSONResponse:
        store: KeyStore = request.app.state.store
        claim = _authorize_claim(request, claim_id)
        if claim is None:
            return _public_error("邮箱记录不存在或查看凭证已失效。", status.HTTP_404_NOT_FOUND)
        if claim.revoked_at:
            return _public_error("这个邮箱记录已被管理员撤销。", status.HTTP_410_GONE)
        card = store.get_redemption_card(claim.card_id)
        if card is None:
            return _public_error("兑换卡记录不存在。", status.HTTP_410_GONE)

        if claim.status == "pending":
            try:
                matched = _find_claim_code(
                    request,
                    claim,
                    get_cloudmail_client=get_cloudmail_client,
                    get_cloudmail_settings=get_cloudmail_settings,
                    get_verification_extractor=get_verification_extractor,
                )
            except (CloudMailError, VerificationExtractionError) as exc:
                return _public_error(
                    f"为避免漏记已到达的验证码，邮件查询恢复后才能跳过：{exc}",
                    status.HTTP_502_BAD_GATEWAY,
                )
            if matched is not None:
                claim = store.complete_registration_claim(
                    claim.id,
                    card_code=card.code,
                    verification_code=matched.code,
                    email_id=matched.email_id,
                )
                refreshed_card = store.get_redemption_card(card.id) or card
                return _public_json(
                    {
                        "claim": _serialize_claim(
                            store,
                            claim,
                            latest_code=matched.code,
                            latest_code_at=matched.create_time,
                        ),
                        "remaining_uses": refreshed_card.remaining_uses,
                        "cooldown_until": _utc_iso(refreshed_card.cooldown_until),
                        "message": "验证码已经到达，本次已按成功接码记录并从当前区收起。",
                    }
                )
            claim = store.skip_registration_claim(
                claim.id,
                card_code=card.code,
                skip_limit=request.app.state.settings.redemption_skip_limit,
                cooldown_minutes=request.app.state.settings.redemption_skip_cooldown_minutes,
            )

        refreshed_card = store.get_redemption_card(card.id) or card
        message = (
            "该邮箱已成功接码，本次操作不会退回次数或移除标签。"
            if claim.status == "completed"
            else "已跳过当前邮箱；未收到验证码，因此没有扣除次数。"
        )
        return _public_json(
            {
                "claim": _serialize_claim(store, claim),
                "remaining_uses": refreshed_card.remaining_uses,
                "cooldown_until": _utc_iso(refreshed_card.cooldown_until),
                "message": message,
            }
        )

    @app.get("/admin/cards", response_class=HTMLResponse)
    def admin_cards(
        request: Request,
        q: str = "",
        category_id: int | None = None,
        page: int = 1,
        message: str = "",
        error: str = "",
        create: bool = False,
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        store: KeyStore = request.app.state.store
        page_size = 100
        total_filtered_cards = store.count_redemption_cards(
            category_id=category_id,
            search_query=q,
        )
        page_count = max(1, (total_filtered_cards + page_size - 1) // page_size)
        current_page = min(max(page, 1), page_count)
        page_start = (current_page - 1) * page_size
        page_cards = store.list_redemption_cards(
            category_id=category_id,
            search_query=q,
            limit=page_size,
            offset=page_start,
        )
        batches_by_id = {batch.id: batch for batch in store.list_card_batches(category_id=category_id)}
        cards_by_batch: dict[int, list[RedemptionCard]] = {}
        for card in page_cards:
            cards_by_batch.setdefault(card.batch_id, []).append(card)
        rendered_batches = [
            _serialize_admin_batch(batches_by_id[batch_id], cards)
            for batch_id, cards in cards_by_batch.items()
            if batch_id in batches_by_id
        ]
        now = _now_db()
        summary = store.get_redemption_card_summary(now=now)
        recent_claim_records = store.list_registration_claims(limit=100)
        card_codes: dict[int, str] = {}
        for claim in recent_claim_records:
            if claim.card_id not in card_codes:
                claim_card = store.get_redemption_card(claim.card_id)
                card_codes[claim.card_id] = claim_card.code if claim_card is not None else ""
        recent_claims = [
            {
                "id": claim.id,
                "card_code": card_codes.get(claim.card_id, ""),
                "recipient_email": claim.recipient_email,
                "address_mode_label": "裂变邮箱" if claim.address_mode == "icloud_alias" else "固定邮箱",
                "status": claim.status,
                "status_label": {
                    "pending": "接码中",
                    "completed": "已成功",
                    "skipped": "已跳过",
                    "timed_out": "已超时",
                }.get(claim.status, claim.status),
                "created_at": claim.created_at,
                "revoked": bool(claim.revoked_at),
                "can_revoke": not claim.revoked_at and claim.status in {"pending", "completed"},
            }
            for claim in recent_claim_records
        ]
        all_tags = store.list_tag_options()
        tags = [tag for tag in all_tags if tag.kind != "system"]
        service_tags = [tag for tag in tags if tag.kind == "service"]
        inventory_tags = [tag for tag in tags if tag.kind == "business"]
        unused_tag = next((tag for tag in all_tags if tag.name == "未使用"), None)
        if unused_tag is not None:
            inventory_tags.insert(0, unused_tag)
        return render(
            request,
            "admin_cards.html",
            {
                "card_categories": store.list_card_categories(),
                "tags": tags,
                "service_tags": service_tags,
                "inventory_tags": inventory_tags,
                "batches": rendered_batches,
                "recent_claims": recent_claims,
                "card_summary": summary,
                "search_query": q,
                "current_category_id": category_id,
                "current_page": current_page,
                "page_count": page_count,
                "total_filtered_cards": total_filtered_cards,
                "message": message or None,
                "error": error or None,
                "create_form_open": create,
            },
            status.HTTP_200_OK,
        )

    @app.post("/admin/card-categories")
    def admin_create_card_category(request: Request, name: str = Form(...)) -> Response:
        if not _is_admin(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        try:
            request.app.state.store.create_card_category(name)
        except ValueError as exc:
            return _admin_redirect("/admin/cards", error=_translate_admin_error(str(exc)))
        return _admin_redirect("/admin/cards", message="兑换卡分类已创建。")

    @app.post("/admin/cards/batches")
    def admin_create_card_batch(
        request: Request,
        name: str = Form(""),
        category_id: str = Form(""),
        target_tag_id: str = Form(""),
        source_tag_id: str = Form(""),
        card_count: int = Form(...),
        uses_per_card: int = Form(...),
        delivery_mode: str = Form("custom"),
        address_mode: str = Form("primary"),
        source_scope: str = Form("all_reusable"),
        include_tag_ids: list[int] = Form([]),
        exclude_tag_ids: list[int] = Form([]),
        expires_at: str = Form(""),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        store: KeyStore = request.app.state.store
        try:
            if delivery_mode.strip().lower() == "independent":
                resolved_target_tag_id = store.ensure_independent_system_tag().id
                product_label = "独立邮箱"
            else:
                resolved_target_tag_id = int(target_tag_id)
                target_tag = store.get_tag(resolved_target_tag_id)
                if target_tag is None or target_tag.kind != "service":
                    raise ValueError("target tag not found")
                product_label = f"{target_tag.name} 定制邮箱"

            normalized_categories = store.list_card_categories()
            if category_id.strip():
                resolved_category_id = int(category_id)
            elif normalized_categories:
                resolved_category_id = normalized_categories[0].id
            else:
                resolved_category_id = store.create_card_category("默认分类").id

            if not source_tag_id.strip():
                raise ValueError("source tag is required")
            normalized_source_tag_id = int(source_tag_id)
            source_tag = store.get_tag(normalized_source_tag_id)
            if source_tag is None or (
                source_tag.kind != "business" and source_tag.name != "未使用"
            ) or source_tag.archived:
                raise ValueError("source tag not found")
            resolved_include_tag_ids = list(include_tag_ids)
            if normalized_source_tag_id not in resolved_include_tag_ids:
                resolved_include_tag_ids.append(normalized_source_tag_id)

            resolved_name = name.strip() or f"{product_label} {datetime.now().strftime('%Y%m%d-%H%M')}"
            batch, cards = store.create_card_batch(
                name=resolved_name,
                category_id=resolved_category_id,
                target_tag_id=resolved_target_tag_id,
                card_count=card_count,
                uses_per_card=uses_per_card,
                delivery_mode=delivery_mode,
                address_mode=address_mode,
                source_scope=source_scope,
                include_tag_ids=resolved_include_tag_ids,
                exclude_tag_ids=exclude_tag_ids,
                expires_at=expires_at,
                expiry_timezone=get_cloudmail_settings(request).display_timezone,
            )
        except (TypeError, ValueError) as exc:
            return _admin_redirect(
                "/admin/cards",
                error=_translate_admin_error(str(exc)),
                create="1",
            )
        return _admin_redirect(
            "/admin/cards",
            message=f"批次“{batch.name}”已生成 {len(cards)} 张兑换卡。",
        )

    @app.get("/admin/cards/batches/{batch_id}/export.txt")
    def admin_export_card_batch(request: Request, batch_id: int) -> Response:
        if not _is_admin(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        store: KeyStore = request.app.state.store
        batch = store.get_card_batch(batch_id)
        if batch is None:
            return Response("批次不存在", status_code=status.HTTP_404_NOT_FOUND)
        cards = store.list_redemption_cards(batch_id=batch_id)
        content = "\ufeff" + "\n".join(card.code for card in cards) + "\n"
        safe_name = "".join(character for character in batch.name if character not in '\\/:*?"<>|') or "兑换卡"
        return Response(
            content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_name)}.txt"},
        )

    @app.post("/admin/claims/{claim_id}/revoke")
    def admin_revoke_claim(request: Request, claim_id: int) -> Response:
        if not _is_admin(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        try:
            request.app.state.store.revoke_registration_claim(claim_id)
        except ValueError as exc:
            return _admin_redirect("/admin/cards", error=_translate_admin_error(str(exc)))
        return _admin_redirect("/admin/cards", message="近期邮箱的继续接码权限已撤销。")

    @app.get("/admin/tags", response_class=HTMLResponse)
    def admin_tags(
        request: Request,
        message: str = "",
        error: str = "",
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        tags: list[dict[str, Any]] = []
        for tag in request.app.state.store.list_tag_options(include_archived=True):
            if tag.kind == "system":
                continue
            effective_rule = build_platform_rule(
                tag.name,
                sender_patterns=tag.sender_patterns,
                subject_keywords=tag.subject_keywords,
            )
            uses_inferred_rule = not tag.sender_patterns and not tag.subject_keywords
            if tag.kind != "service":
                effective_rule_summary = "业务标签不参与平台邮件筛选。"
            else:
                effective_parts: list[str] = []
                if effective_rule.sender_patterns:
                    effective_parts.append(f"发件人 {'、'.join(effective_rule.sender_patterns)}")
                if effective_rule.subject_keywords:
                    effective_parts.append(f"主题 {'、'.join(effective_rule.subject_keywords)}")
                rule_source = "内置" if uses_inferred_rule else "自定义"
                effective_rule_summary = (
                    f"当前生效（{rule_source}）：{'；'.join(effective_parts)}。"
                    if effective_parts
                    else f"当前生效（{rule_source}）：不限制发件人和主题。"
                )
            tags.append(
                {
                "id": tag.id,
                "name": tag.name,
                "color": tag.color,
                "archived": tag.archived,
                "kind": tag.kind,
                "mapping_count": tag.count,
                "success_count": getattr(tag, "success_count", 0),
                "sender_patterns": "\n".join(tag.sender_patterns),
                "subject_keywords": "\n".join(tag.subject_keywords),
                "code_patterns": "\n".join(tag.code_patterns),
                "extraction_mode": tag.extraction_mode,
                "prevents_reuse": tag.prevents_reuse,
                "alias_use_limit": tag.alias_use_limit,
                "uses_inferred_rule": uses_inferred_rule,
                "effective_rule_summary": effective_rule_summary,
                }
            )
        return render(
            request,
            "admin_tags.html",
            {"tags": tags, "message": message or None, "error": error or None},
            status.HTTP_200_OK,
        )

    @app.post("/admin/tags")
    def admin_create_tag(
        request: Request,
        name: str = Form(...),
        color: str = Form(""),
        kind: str = Form("service"),
        sender_patterns: str = Form(""),
        subject_keywords: str = Form(""),
        code_patterns: str = Form(""),
        extraction_mode: str = Form("rules"),
        prevents_reuse: bool = Form(False),
        alias_use_limit: str = Form("0"),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        try:
            normalized_alias_use_limit = _parse_alias_use_limit(alias_use_limit)
            request.app.state.store.create_tag(
                name,
                color,
                kind=kind,
                sender_patterns=sender_patterns,
                subject_keywords=subject_keywords,
                code_patterns=code_patterns,
                extraction_mode=extraction_mode,
                prevents_reuse=prevents_reuse,
                alias_use_limit=normalized_alias_use_limit,
            )
        except ValueError as exc:
            return _admin_redirect("/admin/tags", error=_translate_admin_error(str(exc)))
        return _admin_redirect("/admin/tags", message="标签已创建。")

    @app.post("/admin/tags/{tag_id}/update")
    def admin_update_tag(
        request: Request,
        tag_id: int,
        name: str = Form(...),
        color: str = Form(""),
        kind: str = Form("service"),
        sender_patterns: str | None = Form(None),
        subject_keywords: str | None = Form(None),
        code_patterns: str | None = Form(None),
        extraction_mode: str | None = Form(None),
        prevents_reuse: bool = Form(False),
        alias_use_limit: str = Form("0"),
    ) -> Response:
        if not _is_admin(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        try:
            normalized_alias_use_limit = _parse_alias_use_limit(alias_use_limit)
            request.app.state.store.rename_tag(
                tag_id,
                name,
                color,
                kind=kind,
                sender_patterns=sender_patterns,
                subject_keywords=subject_keywords,
                code_patterns=code_patterns,
                extraction_mode=extraction_mode,
                prevents_reuse=prevents_reuse,
                alias_use_limit=normalized_alias_use_limit,
            )
        except ValueError as exc:
            return _admin_redirect("/admin/tags", error=_translate_admin_error(str(exc)))
        return _admin_redirect("/admin/tags", message="标签已更新。")

    @app.post("/admin/tags/{tag_id}/archive")
    def admin_archive_tag(request: Request, tag_id: int) -> Response:
        if not _is_admin(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        store: KeyStore = request.app.state.store
        tag = store.get_tag(tag_id)
        if tag is None or tag.kind == "system":
            return _admin_redirect("/admin/tags", error="标签不存在。")
        try:
            store.set_tag_archived(tag_id, not tag.archived)
        except ValueError as exc:
            return _admin_redirect("/admin/tags", error=_translate_admin_error(str(exc)))
        return _admin_redirect("/admin/tags", message="标签状态已更新。")

    @app.post("/admin/tags/{tag_id}/delete")
    def admin_delete_tag(request: Request, tag_id: int) -> Response:
        if not _is_admin(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        store: KeyStore = request.app.state.store
        try:
            store.delete_tag(tag_id)
        except ValueError as exc:
            return _admin_redirect("/admin/tags", error=_translate_admin_error(str(exc)))
        return _admin_redirect("/admin/tags", message="标签已彻底删除。")


def _get_session_card(request: Request) -> RedemptionCard | None:
    code = str(request.session.get("redemption_card_code") or "").strip()
    return request.app.state.store.get_card_by_code(code) if code else None


def _card_unavailable_error(card: RedemptionCard) -> tuple[str, int] | None:
    """返回不能建立公开会话的原因；领取时存储层会再次原子校验。"""

    if card.status != "active":
        return "兑换卡已停用。", status.HTTP_403_FORBIDDEN
    if card.remaining_uses <= 0:
        return "兑换卡可用次数已耗尽。", status.HTTP_410_GONE
    if card.expires_at and card.expires_at <= _now_db():
        return "兑换卡已过期。", status.HTTP_410_GONE
    return None


def _authorize_claim(request: Request, claim_id: int) -> RegistrationClaim | None:
    store: KeyStore = request.app.state.store
    claim = store.get_registration_claim(claim_id)
    if claim is None or not claim.baseline_ready:
        return None
    session_card = _get_session_card(request)
    if session_card is not None and session_card.id == claim.card_id and claim.status == "pending":
        return claim
    token = request.headers.get("X-Claim-Token", "").strip()
    return store.get_registration_claim_by_token(claim_id, token)


def _find_claim_code(
    request: Request,
    claim: RegistrationClaim,
    *,
    get_cloudmail_client: Callable[[Request], Any],
    get_cloudmail_settings: Callable[[Request], Any],
    get_verification_extractor: Callable[[Request], Any],
) -> CodeMatch | None:
    settings = get_cloudmail_settings(request)
    messages = get_cloudmail_client(request).fetch_recent_emails(
        claim.query_email,
        limit=settings.recent_email_limit,
    )
    tag = request.app.state.store.get_tag(claim.target_tag_id)
    fallback_email = (
        claim.root_email
        if claim.address_mode == "icloud_alias" and not claim.superseded_at
        else ""
    )
    return find_latest_code(
        messages,
        actual_email=claim.recipient_email,
        claimed_at=claim.created_at,
        baseline_email_id=claim.baseline_email_id,
        platform_rule=_platform_rule(tag),
        fallback_email=fallback_email,
        allow_recipient_fallback=bool(fallback_email),
        code_extractor=get_verification_extractor(request),
    )


def _prime_claim_email_baseline(
    request: Request,
    claim: RegistrationClaim,
    *,
    get_cloudmail_client: Callable[[Request], Any],
    get_cloudmail_settings: Callable[[Request], Any],
) -> RegistrationClaim:
    """记录邮箱交付前已存在的最大邮件编号。"""

    settings = get_cloudmail_settings(request)
    messages = get_cloudmail_client(request).fetch_recent_emails(
        claim.query_email,
        limit=settings.recent_email_limit,
    )
    baseline = max_email_id(messages, baseline=claim.baseline_email_id)
    return request.app.state.store.update_registration_claim_baseline(claim.id, baseline)


def _platform_rule(tag: TagOption | None) -> PlatformRule:
    if tag is None or tag.kind == "system":
        return PlatformRule()
    return build_platform_rule(
        tag.name,
        sender_patterns=tag.sender_patterns,
        subject_keywords=tag.subject_keywords,
        code_patterns=tag.code_patterns,
        extraction_mode=tag.extraction_mode,
    )


def _serialize_card_session(store: KeyStore, card: RedemptionCard) -> dict[str, Any]:
    batch = store.get_card_batch(card.batch_id)
    if batch is None:
        return {"valid": False}
    if batch.delivery_mode == "independent":
        modes = ["primary"]
        product_name = "独立邮箱"
        service_label = ""
    else:
        modes = ["primary", "icloud_alias"] if batch.address_mode == "choice" else [batch.address_mode]
        product_name = f"{batch.target_tag_name} 定制邮箱"
        service_label = batch.target_tag_name
    active_claim = store.get_pending_card_claim(card.code)
    card_payload = {
        "id": card.id,
        "batch_name": batch.name,
        "product_name": product_name,
        "product_type": batch.delivery_mode,
        "delivery_mode": batch.delivery_mode,
        "platform": service_label,
        "service_label": service_label,
        "address_mode": batch.address_mode,
        "address_modes": modes,
        "remaining_uses": card.remaining_uses,
        "total_uses": card.total_uses,
        "expires_at": _utc_iso(card.expires_at),
        "cooldown_until": _utc_iso(card.cooldown_until),
        "consecutive_skips": card.consecutive_skips,
    }
    return {
        "valid": True,
        "card": card_payload,
        "remaining_uses": card.remaining_uses,
        "total_uses": card.total_uses,
        "expires_at": _utc_iso(card.expires_at),
        "cooldown_until": _utc_iso(card.cooldown_until),
        "address_modes": modes,
        "active_claim": (
            _serialize_claim(store, active_claim)
            if active_claim is not None and active_claim.baseline_ready
            else None
        ),
    }


def _serialize_claim(
    store: KeyStore,
    claim: RegistrationClaim,
    *,
    latest_code: str | None = None,
    latest_code_at: str | None = None,
) -> dict[str, Any]:
    card = store.get_redemption_card(claim.card_id)
    batch = store.get_card_batch(card.batch_id) if card is not None else None
    service_label = ""
    delivery_mode = ""
    if batch is not None and batch.delivery_mode == "custom":
        service_label = batch.target_tag_name
    if batch is not None:
        delivery_mode = batch.delivery_mode
    return {
        "id": claim.id,
        "recipient_email": claim.recipient_email,
        "parent_email": claim.root_email,
        "address_mode": claim.address_mode,
        "delivery_mode": delivery_mode,
        "service_label": service_label,
        "status": claim.status,
        "view_token": claim.view_token,
        "created_at": _utc_iso(claim.created_at),
        "completed_at": _utc_iso(claim.completed_at),
        "superseded_at": _utc_iso(claim.superseded_at),
        "live_polling": not bool(claim.revoked_at or claim.superseded_at)
        and claim.status in {"pending", "completed"},
        "latest_code": claim.verification_code if latest_code is None else latest_code,
        "latest_code_at": _utc_iso(claim.completed_at if latest_code_at is None else latest_code_at),
    }


def _serialize_admin_batch(batch: CardBatch, cards: list[RedemptionCard]) -> dict[str, Any]:
    return {
        "id": batch.id,
        "name": batch.name,
        "category_id": batch.category_id,
        "category_name": batch.category_name,
        "target_tag_name": batch.target_tag_name,
        "delivery_mode": batch.delivery_mode,
        "address_mode": batch.address_mode,
        "uses_per_card": batch.uses_per_card,
        "created_at": batch.created_at,
        "cards": [
            {
                "id": card.id,
                "code": card.code,
                "masked_code": card.code,
                "remaining_uses": card.remaining_uses,
                "total_uses": card.total_uses,
                "status": card.status,
                "status_label": "可用" if card.status == "active" else "已停用",
                "last_used_at": "",
            }
            for card in cards
        ],
    }


def _public_json(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


def _public_error(message: str, status_code: int, **extra: Any) -> JSONResponse:
    return _public_json(
        {"error": {"message": message}, "message": message, **extra},
        status_code=status_code,
    )


def _translate_public_store_error(message: str) -> tuple[str, int]:
    translations = {
        "card not found": ("兑换卡不存在。", status.HTTP_404_NOT_FOUND),
        "card is disabled": ("兑换卡已停用。", status.HTTP_403_FORBIDDEN),
        "card has no remaining uses": ("兑换卡可用次数已耗尽。", status.HTTP_410_GONE),
        "card is expired": ("兑换卡已过期。", status.HTTP_410_GONE),
        "card is cooling down": ("连续跳过次数较多，请在冷却结束后再生成邮箱。", status.HTTP_429_TOO_MANY_REQUESTS),
        "no available mapping": ("当前没有符合条件的可用邮箱，请稍后再试。", status.HTTP_409_CONFLICT),
        "address mode is not allowed by this card": ("这张兑换卡不支持所选邮箱方式。", status.HTTP_400_BAD_REQUEST),
        "independent delivery only supports primary addresses": ("独立邮箱不支持裂变模式。", status.HTTP_400_BAD_REQUEST),
        "invalid address mode": ("邮箱方式无效。", status.HTTP_400_BAD_REQUEST),
        "registration claim is not pending": ("邮箱领取已经结束。", status.HTTP_409_CONFLICT),
        "registration claim not found": ("邮箱记录不存在。", status.HTTP_404_NOT_FOUND),
        "registration claim is superseded": ("该邮箱族已有更新的领取地址，当前记录只保留历史验证码。", status.HTTP_409_CONFLICT),
        "registration claim is revoked": ("该邮箱记录已被管理员撤销。", status.HTTP_410_GONE),
        "email_id must be positive": ("验证码邮件缺少可记账的邮件编号。", status.HTTP_409_CONFLICT),
    }
    return translations.get(message, ("当前操作无法完成，请稍后重试。", status.HTTP_409_CONFLICT))


def _translate_admin_error(message: str) -> str:
    translations = {
        "card category name is required": "兑换卡分类名称不能为空。",
        "tag name is required": "标签名称不能为空。",
        "tag already exists": "这个标签已经存在。",
        "tag kind is invalid": "标签用途无效。",
        "tag not found": "标签不存在。",
        "tag is in use": "该标签已有邮箱、接码流水或兑换卡配置，请使用归档保留历史。",
        "system tag cannot be deleted": "系统标签不能删除。",
        "card batch name is required": "批次名称不能为空。",
        "batch name is required": "批次名称不能为空。",
        "target tag not found": "请选择有效的平台标签。",
        "source tag not found": "请选择有效的业务型库存标签。",
        "source tag is required": "请选择邮箱库存标签。",
        "card category not found": "请选择有效的兑换卡分类。",
        "invalid address mode": "领取方式无效。",
        "invalid source scope": "邮箱来源范围无效。",
        "invalid expiry": "有效期格式无效。",
        "tag filters conflict": "同一个标签不能同时设为必须包含和必须排除。",
        "alias_use_limit must be a non-negative integer": "裂变接码上限必须是 0 或正整数。",
    }
    return translations.get(message, message)


def _parse_alias_use_limit(value: str) -> int:
    try:
        normalized = int((value or "0").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("alias_use_limit must be a non-negative integer") from exc
    if normalized < 0:
        raise ValueError("alias_use_limit must be a non-negative integer")
    return normalized


def _admin_redirect(path: str, *, message: str = "", error: str = "", **params: str) -> RedirectResponse:
    query_parts: list[str] = []
    if message:
        query_parts.append(f"message={quote(message)}")
    if error:
        query_parts.append(f"error={quote(error)}")
    query_parts.extend(f"{quote(str(key))}={quote(str(value))}" for key, value in params.items())
    target = path + ("?" + "&".join(query_parts) if query_parts else "")
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _now_db() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _utc_iso(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return normalized
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
