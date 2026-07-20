from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from app.cloudmail import CloudMailError, CloudMailMessage
from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore, RegistrationClaim


class MutableCloudMailClient:
    """可在测试过程中追加邮件的 CloudMail 替身。"""

    def __init__(self) -> None:
        self.messages: list[CloudMailMessage] = []
        self.calls: list[tuple[str, int]] = []
        self.error = ""

    def fetch_recent_emails(self, recipient_email: str, limit: int = 10) -> list[CloudMailMessage]:
        self.calls.append((recipient_email, limit))
        if self.error:
            raise CloudMailError(self.error)
        return self.messages[:limit]

    def add_message(
        self,
        *,
        recipient_email: str,
        code: str,
        email_id: int,
        sender: str = "noreply@tm.openai.com",
        subject: str = "Your ChatGPT verification code",
        create_time: str = "2099-01-01 00:00:00",
    ) -> None:
        self.messages.insert(
            0,
            CloudMailMessage(
                email_id=email_id,
                send_email=sender,
                send_name="Service",
                subject=subject,
                to_email=recipient_email,
                to_name="buyer",
                create_time=create_time,
                type=0,
                content=f"<p>Your verification code is {code}</p>",
                text=f"Your verification code is {code}",
                is_del=0,
            ),
        )


def _settings(tmp_path) -> AppSettings:
    return AppSettings(
        app_secret_key="public-redemption-test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=10,
    )


def _create_card(
    store: KeyStore,
    *,
    address_mode: str = "primary",
    delivery_mode: str = "custom",
    uses: int = 3,
    expires_at: str = "",
):
    tag = (
        store.ensure_independent_system_tag()
        if delivery_mode == "independent"
        else store.create_tag(
            "GPT",
            kind="service",
            sender_patterns="@tm.openai.com",
            subject_keywords="chatgpt",
        )
    )
    category = store.create_card_category("公开注册台")
    _batch, cards = store.create_card_batch(
        name="公开注册台测试批次",
        category_id=category.id,
        target_tag_id=tag.id,
        card_count=1,
        uses_per_card=uses,
        delivery_mode=delivery_mode,
        address_mode=address_mode,
        source_scope="never_used" if delivery_mode == "independent" else "all_reusable",
        expires_at=expires_at,
    )
    return tag, cards[0]


def test_public_redeem_rejects_expired_card(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    _target_tag, card = _create_card(store, expires_at="2000-01-01T00:00:00Z")
    client = TestClient(create_app(settings=_settings(tmp_path), store=store))

    response = client.post("/api/public/redeem", json={"card_code": card.code})

    assert response.status_code == 410
    assert response.json()["message"] == "兑换卡已过期。"


def test_public_session_is_cleared_when_card_becomes_exhausted(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping("exhausted@icloud.com")
    _target_tag, card = _create_card(store, uses=1)
    client = TestClient(create_app(settings=_settings(tmp_path), store=store))
    assert client.post("/api/public/redeem", json={"card_code": card.code}).status_code == 200
    claim = store.start_registration_claim(card.code)
    store.complete_registration_claim(
        claim.id,
        card_code=card.code,
        verification_code="123456",
        email_id=1,
    )

    restored = client.get("/api/public/session")
    redeemed_again = client.post("/api/public/redeem", json={"card_code": card.code})

    assert restored.status_code == 200
    assert restored.json() == {"valid": False, "message": "兑换卡可用次数已耗尽。"}
    assert redeemed_again.status_code == 410


def _claim_from(payload: dict[str, Any]) -> dict[str, Any]:
    claim = payload.get("claim") or payload.get("mapping") or payload
    assert isinstance(claim, dict)
    return claim


def _remaining_uses(payload: dict[str, Any]) -> int:
    card = payload.get("card") if isinstance(payload.get("card"), dict) else {}
    value = payload.get("remaining_uses", card.get("remaining_uses"))
    assert isinstance(value, int)
    return value


def _claim_id(claim: dict[str, Any]) -> int:
    value = claim.get("id", claim.get("claim_id"))
    assert isinstance(value, int)
    return value


def _claim_email(claim: dict[str, Any]) -> str:
    value = claim.get("recipient_email", claim.get("registration_email", claim.get("email")))
    assert isinstance(value, str) and value
    return value


def _claim_token(claim: dict[str, Any]) -> str:
    value = claim.get("view_token", claim.get("claim_token"))
    assert isinstance(value, str) and value
    return value


def test_independent_claim_keeps_product_type_in_recent_mailbox_payload(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping("independent-recent@icloud.com")
    _target_tag, card = _create_card(store, delivery_mode="independent")
    client = TestClient(
        create_app(settings=_settings(tmp_path), store=store, cloudmail_client=MutableCloudMailClient())
    )

    assert client.post("/api/public/redeem", json={"card_code": card.code}).status_code == 200
    response = client.post("/api/public/claims", json={"address_mode": "primary"})

    assert response.status_code == 201
    assert _claim_from(response.json())["delivery_mode"] == "independent"


def test_public_claim_skips_reserved_example_domain_inventory(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping("legacy-browser-test@example.com")
    real_mapping = store.create_mapping("real-inventory@icloud.com")
    _target_tag, card = _create_card(store, delivery_mode="independent")

    claim = store.start_registration_claim(card.code)

    assert claim.recipient_email == real_mapping.recipient_email


def test_home_is_public_redemption_workbench_and_key_lookup_stays_available(tmp_path) -> None:
    client = TestClient(create_app(settings=_settings(tmp_path), cloudmail_client=MutableCloudMailClient()))

    home = client.get("/")
    key_lookup = client.get("/key-lookup")

    assert home.status_code == 200
    assert "data-public-workbench" in home.text
    assert "验证兑换卡" in home.text
    assert key_lookup.status_code == 200
    assert "查看 Key" in key_lookup.text


def test_code_is_charged_only_after_exact_recipient_and_platform_match(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping("root@icloud.com")
    target_tag, card = _create_card(store, uses=2)
    cloudmail = MutableCloudMailClient()
    client = TestClient(create_app(settings=_settings(tmp_path), store=store, cloudmail_client=cloudmail))

    redeemed = client.post("/api/public/redeem", json={"card_code": card.code})
    assert redeemed.status_code == 200
    assert _remaining_uses(redeemed.json()) == 2
    # 服务端会话已保存卡密，响应不应再次回显完整卡密。
    assert card.code not in json.dumps(redeemed.json(), ensure_ascii=False)

    generated = client.post("/api/public/claims", json={"address_mode": "primary"})
    assert generated.status_code in {200, 201}
    claim = _claim_from(generated.json())
    claim_id = _claim_id(claim)
    claim_token = _claim_token(claim)
    assert _claim_email(claim) == mapping.recipient_email
    assert store.get_card_by_code(card.code).remaining_uses == 2
    assert store.list_mapping_tags(mapping.id) == []

    # 错平台、错收件人的邮件均不能让邮箱变成“已使用”。
    cloudmail.add_message(
        recipient_email=mapping.recipient_email,
        code="111111",
        email_id=1,
        sender="noreply@anthropic.com",
        subject="Claude verification code",
    )
    cloudmail.add_message(
        recipient_email="someone-else@icloud.com",
        code="222222",
        email_id=2,
    )
    waiting = client.get(f"/api/public/claims/{claim_id}/code")
    assert waiting.status_code == 200
    assert not waiting.json().get("latest_code")
    assert store.get_card_by_code(card.code).remaining_uses == 2
    assert store.list_mapping_tags(mapping.id) == []

    cloudmail.add_message(recipient_email=mapping.recipient_email, code="330119", email_id=3)
    completed = client.get(f"/api/public/claims/{claim_id}/code")
    repeated = client.get(
        f"/api/public/claims/{claim_id}/code",
        headers={"X-Claim-Token": claim_token},
    )

    assert completed.status_code == 200
    assert completed.json()["latest_code"] == "330119"
    assert repeated.json()["latest_code"] == "330119"
    assert store.get_card_by_code(card.code).remaining_uses == 1
    assert [tag.id for tag in store.list_mapping_tags(mapping.id)] == [target_tag.id]

    # 退出卡密会话后，浏览器近期记录仍可凭不透明 claim token 持续接码。
    assert client.delete("/api/public/session").status_code == 200
    restored = client.get(
        f"/api/public/claims/{claim_id}/code",
        headers={"X-Claim-Token": claim_token},
    )
    denied = client.get(f"/api/public/claims/{claim_id}/code")
    assert restored.status_code == 200
    assert restored.json()["latest_code"] == "330119"
    assert denied.status_code in {401, 403, 404}


def test_public_claim_records_preexisting_email_baseline(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping("baseline@icloud.com")
    _target_tag, card = _create_card(store, uses=2)
    cloudmail = MutableCloudMailClient()
    cloudmail.add_message(
        recipient_email=mapping.recipient_email,
        code="119900",
        email_id=77,
        create_time="2000-01-01 00:00:00",
    )
    client = TestClient(create_app(settings=_settings(tmp_path), store=store, cloudmail_client=cloudmail))

    assert client.post("/api/public/redeem", json={"card_code": card.code}).status_code == 200
    generated = client.post("/api/public/claims", json={"address_mode": "primary"})
    claim_payload = _claim_from(generated.json())
    claim = store.get_registration_claim(_claim_id(claim_payload))

    assert generated.status_code == 201
    assert generated.json()["baseline_ready"] is True
    assert claim is not None
    assert claim.baseline_email_id == 77
    waiting = client.get(f"/api/public/claims/{claim.id}/code")
    assert waiting.status_code == 200
    assert waiting.json()["latest_code"] == ""
    assert store.get_card_by_code(card.code).remaining_uses == 2


def test_public_claim_hides_mailbox_until_failed_snapshot_recovers(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping("snapshot-retry@icloud.com")
    _target_tag, card = _create_card(store, uses=2)
    cloudmail = MutableCloudMailClient()
    cloudmail.error = "upstream unavailable"
    client = TestClient(create_app(settings=_settings(tmp_path), store=store, cloudmail_client=cloudmail))

    assert client.post("/api/public/redeem", json={"card_code": card.code}).status_code == 200
    failed = client.post("/api/public/claims", json={"address_mode": "primary"})
    pending = store.get_pending_card_claim(card.code)

    assert failed.status_code == 502
    assert failed.json()["baseline_ready"] is False
    assert "claim" not in failed.json()
    assert pending is not None and not pending.baseline_ready
    assert client.get("/api/public/session").json()["active_claim"] is None

    cloudmail.error = ""
    restored = client.post("/api/public/claims", json={"address_mode": "primary"})
    restored_claim = _claim_from(restored.json())

    assert restored.status_code == 201
    assert _claim_id(restored_claim) == pending.id
    assert restored.json()["baseline_ready"] is True
    assert store.get_registration_claim(pending.id).baseline_ready


def test_public_alias_snapshot_separates_old_and_new_root_messages_in_same_second(
    tmp_path,
    monkeypatch,
) -> None:
    """领取前已存在的同秒邮件不能命中，领取后更大的邮件编号仍可命中。"""

    monkeypatch.setattr(KeyStore, "_now", staticmethod(lambda: "2026-07-18 10:00:00"))
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping("same-second-public@icloud.com")
    _target_tag, card = _create_card(store, address_mode="icloud_alias", uses=2)
    cloudmail = MutableCloudMailClient()
    cloudmail.add_message(
        recipient_email=mapping.recipient_email,
        code="101101",
        email_id=101,
        create_time="2026-07-18 10:00:00",
    )
    client = TestClient(create_app(settings=_settings(tmp_path), store=store, cloudmail_client=cloudmail))

    assert client.post("/api/public/redeem", json={"card_code": card.code}).status_code == 200
    generated = client.post("/api/public/claims", json={"address_mode": "icloud_alias"})
    claim_payload = _claim_from(generated.json())
    claim_id = _claim_id(claim_payload)
    claim = store.get_registration_claim(claim_id)

    assert generated.status_code == 201
    assert generated.json()["baseline_ready"] is True
    assert _claim_email(claim_payload).startswith("same-second-public+")
    assert claim is not None
    assert claim.baseline_email_id == 101
    old_poll = client.get(f"/api/public/claims/{claim_id}/code")
    assert old_poll.status_code == 200
    assert old_poll.json()["latest_code"] == ""

    cloudmail.add_message(
        recipient_email=mapping.recipient_email,
        code="102102",
        email_id=102,
        create_time="2026-07-18 10:00:00",
    )
    new_poll = client.get(f"/api/public/claims/{claim_id}/code")

    assert new_poll.status_code == 200
    assert new_poll.json()["latest_code"] == "102102"
    assert new_poll.json()["recipient_match"] == "root_fallback"
    assert store.get_card_by_code(card.code).remaining_uses == 1


def test_icloud_alias_uses_safe_root_fallback_and_can_repeat_same_service(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    root = store.create_mapping("alias-source@icloud.com")
    target_tag, card = _create_card(store, address_mode="icloud_alias", uses=3)
    cloudmail = MutableCloudMailClient()
    client = TestClient(create_app(settings=_settings(tmp_path), store=store, cloudmail_client=cloudmail))
    assert client.post("/api/public/redeem", json={"card_code": card.code}).status_code == 200

    first_response = client.post("/api/public/claims", json={"address_mode": "icloud_alias"})
    first = _claim_from(first_response.json())
    first_email = _claim_email(first)
    assert first_response.status_code in {200, 201}
    assert first_email.startswith("alias-source+")
    assert first_email.endswith("@icloud.com")

    # CloudMail 丢失 +别名时，当前邮箱族的独占领取可安全回退到主邮箱。
    cloudmail.add_message(recipient_email=root.recipient_email, code="400001", email_id=10)
    fallback_match = client.get(f"/api/public/claims/{_claim_id(first)}/code")
    assert fallback_match.status_code == 200
    assert fallback_match.json()["latest_code"] == "400001"
    assert fallback_match.json()["recipient_match"] == "root_fallback"
    assert store.get_card_by_code(card.code).remaining_uses == 2

    cloudmail.add_message(recipient_email=first_email, code="400002", email_id=11)
    matched = client.get(
        f"/api/public/claims/{_claim_id(first)}/code",
        headers={"X-Claim-Token": first["view_token"]},
    )
    assert matched.json()["latest_code"] == "400002"
    assert store.get_card_by_code(card.code).remaining_uses == 2
    assert [tag.id for tag in store.list_mapping_tags(root.id)] == [target_tag.id]
    assert store.count_verification_events(tag_id=target_tag.id) == 2

    # 根邮箱已有 GPT 标签后，裂变模式仍允许同平台生成全新的地址。
    second_response = client.post("/api/public/claims", json={"address_mode": "icloud_alias"})
    second = _claim_from(second_response.json())
    assert second_response.status_code in {200, 201}
    assert _claim_email(second) != first_email
    assert _claim_email(second).startswith("alias-source+")
    frozen_first = client.get(
        f"/api/public/claims/{_claim_id(first)}/code",
        headers={"X-Claim-Token": first["view_token"]},
    )
    assert frozen_first.status_code == 200
    assert frozen_first.json()["claim"]["live_polling"] is False
    assert "历史" in frozen_first.json()["message"] or "冻结" in frozen_first.json()["message"]

    skipped = client.post(f"/api/public/claims/{_claim_id(second)}/skip")
    assert skipped.status_code == 200
    assert store.get_card_by_code(card.code).remaining_uses == 2
    skipped_claim: RegistrationClaim | None = store.get_registration_claim(_claim_id(second))
    assert skipped_claim is not None
    assert store.get_by_id(skipped_claim.mapping_id).reuse_policy == "retired"


def test_three_no_code_skips_keep_quota_and_start_server_side_cooldown(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mappings = [store.create_mapping(f"cooldown-{index}@icloud.com") for index in range(3)]
    _target_tag, card = _create_card(store, uses=5)
    client = TestClient(
        create_app(settings=_settings(tmp_path), store=store, cloudmail_client=MutableCloudMailClient())
    )
    assert client.post("/api/public/redeem", json={"card_code": card.code}).status_code == 200

    last_skip_payload: dict[str, Any] = {}
    for _ in range(3):
        generated = client.post("/api/public/claims", json={"address_mode": "primary"})
        assert generated.status_code in {200, 201}
        claim = _claim_from(generated.json())
        skipped = client.post(f"/api/public/claims/{_claim_id(claim)}/skip")
        assert skipped.status_code == 200
        last_skip_payload = skipped.json()

    cooled_card = store.get_card_by_code(card.code)
    assert cooled_card is not None
    assert cooled_card.remaining_uses == 5
    assert cooled_card.cooldown_until
    assert last_skip_payload.get("cooldown_until")
    assert all(store.list_mapping_tags(mapping.id) == [] for mapping in mappings)
    assert all(store.get_by_id(mapping.id).first_used_at == "" for mapping in mappings)

    blocked = client.post("/api/public/claims", json={"address_mode": "primary"})
    assert blocked.status_code == 429
    assert blocked.json().get("cooldown_until")
