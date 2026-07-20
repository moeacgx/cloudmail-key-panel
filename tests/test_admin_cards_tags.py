from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


def _admin_client(tmp_path) -> tuple[TestClient, KeyStore]:
    store = KeyStore(tmp_path / "app.db")
    settings = AppSettings(
        app_secret_key="admin-card-test",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    client = TestClient(create_app(settings=settings, store=store))
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})
    return client, store


def test_admin_can_create_platform_tag_card_category_batch_and_export_txt(tmp_path) -> None:
    client, store = _admin_client(tmp_path)

    tag_response = client.post(
        "/admin/tags",
        data={
            "name": "GPT",
            "kind": "service",
            "color": "#22c55e",
            "sender_patterns": "@openai.com\n*@*.openai.com",
            "subject_keywords": "ChatGPT",
        },
        follow_redirects=True,
    )
    category_response = client.post(
        "/admin/card-categories",
        data={"name": "渠道 A"},
        follow_redirects=True,
    )
    tag = store.get_tag(store.get_category_id("GPT"))
    inventory_tag = store.create_tag("待发放", kind="business")
    category = store.list_card_categories()[0]

    assert tag_response.status_code == 200
    assert "GPT" in tag_response.text
    assert tag is not None and tag.sender_patterns == ("@openai.com", "*@*.openai.com")
    assert category_response.status_code == 200
    assert "渠道 A" in category_response.text

    batch_response = client.post(
        "/admin/cards/batches",
        data={
            "name": "GPT 测试批次",
            "category_id": str(category.id),
            "target_tag_id": str(tag.id),
            "source_tag_id": str(inventory_tag.id),
            "card_count": "3",
            "uses_per_card": "5",
            "delivery_mode": "custom",
            "address_mode": "choice",
            "source_scope": "all_reusable",
        },
        follow_redirects=True,
    )
    batch = store.list_card_batches()[0]
    cards = store.list_redemption_cards(batch_id=batch.id)
    exported = client.get(f"/admin/cards/batches/{batch.id}/export.txt")

    assert batch_response.status_code == 200
    assert "已生成 3 张兑换卡" in batch_response.text
    assert "批量复制" in batch_response.text
    assert "批量导出 TXT" in batch_response.text
    assert len(cards) == 3
    assert all(card.total_uses == card.remaining_uses == 5 for card in cards)
    assert exported.status_code == 200
    assert exported.text.lstrip("\ufeff").splitlines() == [card.code for card in cards]
    assert "attachment" in exported.headers["content-disposition"]


def test_admin_can_toggle_global_independent_account_policy_on_tag(tmp_path) -> None:
    client, store = _admin_client(tmp_path)

    created = client.post(
        "/admin/tags",
        data={
            "name": "TG",
            "kind": "service",
            "color": "#22c55e",
            "prevents_reuse": "true",
        },
        follow_redirects=True,
    )
    tag = store.get_tag(store.get_category_id("TG"))

    assert created.status_code == 200
    assert tag is not None and tag.prevents_reuse is True
    assert "设为独立账号标签" in created.text
    assert 'data-prevents-reuse="true"' in created.text
    assert "独立账号" in created.text

    updated = client.post(
        f"/admin/tags/{tag.id}/update",
        data={"name": "Telegram", "kind": "service", "color": "#22c55e"},
        follow_redirects=True,
    )

    assert updated.status_code == 200
    assert store.get_tag(tag.id).prevents_reuse is False
    assert 'data-prevents-reuse="false"' in updated.text

    dashboard = client.get("/admin")
    assert 'class="size-2 shrink-0 rounded-full"' not in dashboard.text


def test_independent_batch_needs_no_public_platform_tag(tmp_path) -> None:
    client, store = _admin_client(tmp_path)
    client.post("/admin/card-categories", data={"name": "隐私交付"})
    inventory_tag = store.create_tag("独立库存", kind="business")
    category = store.list_card_categories()[0]

    response = client.post(
        "/admin/cards/batches",
        data={
            "name": "独立邮箱批次",
            "category_id": str(category.id),
            "target_tag_id": "",
            "source_tag_id": str(inventory_tag.id),
            "card_count": "1",
            "uses_per_card": "1",
            "delivery_mode": "independent",
            "address_mode": "primary",
            "source_scope": "all_reusable",
        },
        follow_redirects=True,
    )
    batch = store.list_card_batches()[0]

    assert response.status_code == 200
    assert batch.delivery_mode == "independent"
    assert batch.source_scope == "never_used"
    assert store.get_tag(batch.target_tag_id).kind == "system"


def test_admin_batch_treats_datetime_local_as_configured_display_timezone(tmp_path) -> None:
    client, store = _admin_client(tmp_path)
    store.save_cloudmail_settings(
        base_url="https://mail.example.com",
        api_token="test-token",
        display_timezone="Asia/Shanghai",
    )
    inventory_tag = store.create_tag("时区库存", kind="business")

    response = client.post(
        "/admin/cards/batches",
        data={
            "delivery_mode": "independent",
            "source_tag_id": str(inventory_tag.id),
            "card_count": "1",
            "uses_per_card": "1",
            "expires_at": "2026-07-21T18:00",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert store.list_card_batches()[0].expires_at == "2026-07-21 10:00:00"


def test_admin_rejects_batch_without_inventory_tag(tmp_path) -> None:
    client, store = _admin_client(tmp_path)

    response = client.post(
        "/admin/cards/batches",
        data={
            "delivery_mode": "independent",
            "card_count": "1",
            "uses_per_card": "1",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "请选择邮箱库存标签" in response.text
    assert store.list_card_batches() == []


def test_admin_cards_paginates_large_batches(tmp_path) -> None:
    client, store = _admin_client(tmp_path)
    inventory_tag = store.create_tag("批量库存", kind="business")
    category = store.create_card_category("分页测试")
    target_tag = store.ensure_independent_system_tag()
    batch, cards = store.create_card_batch(
        name="大批次",
        category_id=category.id,
        target_tag_id=target_tag.id,
        card_count=101,
        uses_per_card=1,
        delivery_mode="independent",
        address_mode="primary",
        include_tag_ids=[inventory_tag.id],
    )

    first_page = client.get("/admin/cards")
    second_page = client.get("/admin/cards?page=2")

    assert first_page.status_code == second_page.status_code == 200
    assert first_page.text.count("<tr data-card-row") == 100
    assert second_page.text.count("<tr data-card-row") == 1
    assert f'href="/admin/cards/batches/{batch.id}/export.txt"' in first_page.text
    assert cards[-1].code in second_page.text


def test_quick_create_independent_batch_uses_inventory_tag_and_automatic_defaults(tmp_path) -> None:
    client, store = _admin_client(tmp_path)
    inventory_tag = store.create_tag("未使用", kind="business")
    page = client.get("/admin/cards")

    assert page.status_code == 200
    assert "只需选择产品、库存和数量即可生成" in page.text
    assert "高级设置（可选）" in page.text
    assert f'value="{inventory_tag.id}" data-count="0"' in page.text

    response = client.post(
        "/admin/cards/batches",
        data={
            "delivery_mode": "independent",
            "source_tag_id": str(inventory_tag.id),
            "card_count": "2",
            "uses_per_card": "1",
        },
        follow_redirects=True,
    )
    batch = store.list_card_batches()[0]

    assert response.status_code == 200
    assert "已生成 2 张兑换卡" in response.text
    assert batch.name.startswith("独立邮箱 ")
    assert batch.category_name == "默认分类"
    assert batch.include_tag_ids == (inventory_tag.id,)


def test_admin_cards_lists_and_revokes_recent_claim_token(tmp_path) -> None:
    client, store = _admin_client(tmp_path)
    mapping = store.create_mapping("revoke@icloud.com")
    tag = store.create_tag("GPT", kind="service")
    card_category = store.create_card_category("撤销测试")
    _batch, cards = store.create_card_batch(
        name="撤销测试批次",
        category_id=card_category.id,
        target_tag_id=tag.id,
        card_count=1,
        uses_per_card=1,
        delivery_mode="custom",
        address_mode="primary",
    )
    claim = store.start_registration_claim(cards[0].code)

    page = client.get("/admin/cards")
    revoked = client.post(f"/admin/claims/{claim.id}/revoke", follow_redirects=True)
    refreshed_claim = store.get_registration_claim(claim.id)

    assert page.status_code == 200
    assert mapping.recipient_email in page.text
    assert f'action="/admin/claims/{claim.id}/revoke"' in page.text
    assert "撤销接码权限" in page.text
    assert revoked.status_code == 200
    assert "继续接码权限已撤销" in revoked.text
    assert refreshed_claim is not None and refreshed_claim.revoked_at
    assert store.get_registration_claim_by_token(claim.id, claim.view_token) is None
