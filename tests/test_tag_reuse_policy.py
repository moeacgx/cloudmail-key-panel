from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


class EmptyMailboxClient:
    def fetch_recent_emails(self, recipient_email: str, limit: int = 10):
        return []


def _create_card(
    store: KeyStore,
    *,
    target_tag_id: int,
    source_tag_id: int,
    address_mode: str = "primary",
):
    card_category = store.create_card_category("标签策略测试卡")
    _batch, cards = store.create_card_batch(
        name=f"标签策略-{address_mode}",
        category_id=card_category.id,
        target_tag_id=target_tag_id,
        card_count=1,
        uses_per_card=2,
        delivery_mode="custom",
        address_mode=address_mode,
        source_scope="all_reusable",
        include_tag_ids=[source_tag_id],
    )
    return cards[0]


def _admin_client(tmp_path, store: KeyStore) -> TestClient:
    settings = AppSettings(
        app_secret_key="tag-policy-test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    client = TestClient(create_app(settings=settings, store=store))
    login = client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )
    assert login.status_code == 200
    return client


def _create_mixed_tag_pool(store: KeyStore):
    """首条同时带 GPT/TG，第二条只带 GPT，便于验证跨标签全局排除。"""

    gpt = store.create_tag("GPT", kind="service")
    tg = store.create_tag("TG", kind="service", prevents_reuse=True)
    gemini = store.create_tag("Gemini", kind="service")
    protected = store.create_mapping("protected@icloud.com", category=gpt.name)
    eligible = store.create_mapping("eligible@icloud.com", category=gpt.name)
    store.add_mapping_tag(protected.id, tg.id, source="usage")
    return gpt, tg, gemini, protected, eligible


def test_tag_prevents_reuse_setting_persists_and_can_be_disabled(tmp_path) -> None:
    database = tmp_path / "app.db"
    store = KeyStore(database)
    tag = store.create_tag("TG", kind="service", prevents_reuse=True)

    assert tag.prevents_reuse is True
    assert KeyStore(database).get_tag(tag.id).prevents_reuse is True

    # 非表单调用只是确保标签存在时，不能因为省略策略参数而意外关闭保护。
    ensured = store.create_tag("TG", kind="service")
    assert ensured.prevents_reuse is True

    # 普通编辑未传该字段时不能误清空策略。
    renamed = store.rename_tag(tag.id, "Telegram")
    assert renamed.prevents_reuse is True

    disabled = store.set_tag_prevents_reuse(tag.id, False)
    assert disabled.prevents_reuse is False
    assert KeyStore(database).get_tag(tag.id).prevents_reuse is False


def test_legacy_database_adds_tag_prevents_reuse_column_with_safe_default(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    original = KeyStore(database)
    tag = original.create_tag("旧标签", kind="business")
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE categories DROP COLUMN prevents_reuse")
        connection.commit()

    migrated = KeyStore(database)

    assert migrated.get_tag(tag.id).prevents_reuse is False
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(categories)")}
    assert "prevents_reuse" in columns


def test_admin_primary_claim_skips_mapping_with_any_protected_tag(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    gpt, tg, gemini, protected, eligible = _create_mixed_tag_pool(store)

    claimed = store.claim_next_available_mapping(
        category_filter=gpt.name,
        target_site=gemini.name,
        claimed_by="admin:primary",
        address_mode="primary",
    )

    assert claimed is not None
    assert claimed.id == eligible.id
    assert protected.id < eligible.id

    # 关闭 TG 的全局独立策略后，邮箱自身仍是 reusable，应该恢复领取资格。
    store.reset_mapping_status(claimed.id, claimed_by="admin:primary")
    store.set_tag_prevents_reuse(tg.id, False)
    restored = store.claim_next_available_mapping(
        category_filter=gpt.name,
        target_site=gemini.name,
        claimed_by="admin:restored",
        address_mode="primary",
    )
    assert restored is not None
    assert restored.id == protected.id


def test_admin_alias_claim_applies_protected_policy_to_root_family(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    gpt, _tg, gemini, protected, eligible = _create_mixed_tag_pool(store)

    claimed = store.claim_next_available_mapping(
        category_filter=gpt.name,
        target_site=gemini.name,
        claimed_by="admin:alias",
        address_mode="icloud_alias",
    )

    assert claimed is not None
    assert claimed.address_kind == "icloud_alias"
    assert claimed.parent_mapping_id == eligible.id
    assert claimed.parent_mapping_id != protected.id


def test_external_api_claim_skips_mapping_with_any_protected_tag(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    gpt, _tg, gemini, protected, eligible = _create_mixed_tag_pool(store)
    settings = AppSettings(
        app_secret_key="tag-policy-api-test",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    client = TestClient(
        create_app(
            settings=settings,
            store=store,
            cloudmail_client=EmptyMailboxClient(),
        )
    )

    response = client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": gpt.id, "target_tag_id": gemini.id},
        headers={"X-Client-ID": "tag-policy-worker"},
        auth=("admin", "pass123"),
    )

    assert response.status_code == 200
    assert response.json()["mapping"]["id"] == eligible.id
    assert response.json()["mapping"]["id"] != protected.id


def test_public_primary_claim_skips_protected_tag_even_when_sourcing_gpt(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    gpt, _tg, gemini, protected, eligible = _create_mixed_tag_pool(store)
    card = _create_card(
        store,
        target_tag_id=gemini.id,
        source_tag_id=gpt.id,
        address_mode="primary",
    )

    claim = store.start_registration_claim(card.code)

    assert claim.mapping_id == eligible.id
    assert claim.root_mapping_id == eligible.id
    assert claim.root_mapping_id != protected.id


def test_public_alias_claim_skips_entire_family_with_protected_tag(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    gpt, _tg, gemini, protected, eligible = _create_mixed_tag_pool(store)
    card = _create_card(
        store,
        target_tag_id=gemini.id,
        source_tag_id=gpt.id,
        address_mode="icloud_alias",
    )

    claim = store.start_registration_claim(card.code)

    assert claim.address_mode == "icloud_alias"
    assert claim.root_mapping_id == eligible.id
    assert claim.root_mapping_id != protected.id
    assert store.get_by_id(claim.mapping_id).parent_mapping_id == eligible.id


def test_removing_manual_protected_tag_restores_dynamic_pool_eligibility(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    gpt = store.create_tag("GPT", kind="service")
    tg = store.create_tag("TG", kind="service", prevents_reuse=True)
    gemini = store.create_tag("Gemini", kind="service")
    protected = store.create_mapping("manual-protected@icloud.com", category=gpt.name)
    eligible = store.create_mapping("manual-eligible@icloud.com", category=gpt.name)
    store.add_mapping_tag(protected.id, tg.id, source="manual")

    first = store.claim_next_available_mapping(
        category_filter=gpt.name,
        target_site=gemini.name,
        claimed_by="admin:manual-first",
    )
    assert first is not None and first.id == eligible.id
    store.reset_mapping_status(first.id, claimed_by="admin:manual-first")

    store.remove_mapping_tag(protected.id, tg.id)
    restored = store.claim_next_available_mapping(
        category_filter=gpt.name,
        target_site=gemini.name,
        claimed_by="admin:manual-restored",
    )

    assert restored is not None and restored.id == protected.id


def test_disabling_tag_policy_does_not_override_explicit_independent_mapping(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    gpt = store.create_tag("GPT", kind="service")
    tg = store.create_tag("TG", kind="service", prevents_reuse=True)
    gemini = store.create_tag("Gemini", kind="service")
    independent = store.create_mapping("explicit-independent@icloud.com", category=gpt.name)
    eligible = store.create_mapping("explicit-eligible@icloud.com", category=gpt.name)
    store.add_mapping_tag(independent.id, tg.id, source="manual")
    store.set_mapping_reuse_policy(independent.id, "independent")
    store.set_tag_prevents_reuse(tg.id, False)

    claimed = store.claim_next_available_mapping(
        category_filter=gpt.name,
        target_site=gemini.name,
        claimed_by="admin:explicit-independent",
    )

    assert claimed is not None and claimed.id == eligible.id


def test_admin_can_enable_and_disable_global_independent_tag_policy(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    client = _admin_client(tmp_path, store)

    created = client.post(
        "/admin/tags",
        data={
            "name": "TG",
            "kind": "service",
            "prevents_reuse": "true",
        },
        follow_redirects=True,
    )
    tag_id = store.get_category_id("TG")

    assert created.status_code == 200
    assert tag_id is not None
    assert store.get_tag(tag_id).prevents_reuse is True
    assert 'data-prevents-reuse="true"' in created.text
    assert "独立账号" in created.text

    disabled = client.post(
        f"/admin/tags/{tag_id}/update",
        data={
            "name": "TG",
            "kind": "service",
            # HTML 未选中的复选框不会提交字段，应解释为关闭策略。
        },
        follow_redirects=True,
    )

    assert disabled.status_code == 200
    assert store.get_tag(tag_id).prevents_reuse is False
    assert 'data-prevents-reuse="false"' in disabled.text


def test_admin_mapping_tag_selector_has_no_decorative_purple_dots(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    tag = store.create_tag("GPT", "#6366f1", kind="service")
    store.create_mapping("tagged@icloud.com", category=tag.name)
    client = _admin_client(tmp_path, store)

    dashboard = client.get("/admin")

    assert dashboard.status_code == 200
    assert 'class="size-2 shrink-0 rounded-full"' not in dashboard.text
