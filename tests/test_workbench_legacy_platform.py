from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


class EmptyMailboxClient:
    def fetch_recent_emails(self, recipient_email: str, limit: int = 10):
        return []


def _client(tmp_path, store: KeyStore) -> TestClient:
    settings = AppSettings(
        app_secret_key="legacy-platform-test",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_api_token="token",
    )
    client = TestClient(
        create_app(settings=settings, store=store, cloudmail_client=EmptyMailboxClient())
    )
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})
    return client


def _clear_claim_platform(store: KeyStore, mapping_id: int) -> None:
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE access_mappings SET target_site = '' WHERE id = ?",
            (int(mapping_id),),
        )
        connection.commit()


def test_admin_refresh_binds_platform_for_pre_upgrade_active_claim(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source_tag = store.create_tag("未使用", kind="business")
    platform_tag = store.create_tag("Chatgpt", kind="service")
    mapping = store.create_mapping("legacy-current@icloud.com", category=source_tag.name)
    client = _client(tmp_path, store)

    claimed = client.post(
        "/api/workbench/claim-next",
        data={"category": source_tag.name, "target_tag_id": platform_tag.id},
    ).json()["mapping"]
    before = store.get_by_id(mapping.id)
    _clear_claim_platform(store, mapping.id)

    response = client.get(
        "/api/workbench/current/mailbox",
        params={"target_tag_id": platform_tag.id},
    )
    rebound = store.get_by_id(mapping.id)

    assert response.status_code == 200
    assert response.json()["mapping"]["id"] == claimed["id"]
    assert rebound.target_site == platform_tag.name
    assert rebound.claimed_at == before.claimed_at
    assert rebound.last_seen_email_id == before.last_seen_email_id
    assert store.is_workbench_claim_baseline_ready(mapping.id, claimed_by=rebound.claimed_by)


def test_admin_alias_next_recovers_pre_upgrade_claim_without_platform(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source_tag = store.create_tag("未使用", kind="business")
    platform_tag = store.create_tag("Chatgpt", kind="service")
    first = store.create_mapping("legacy-first@icloud.com", category=source_tag.name)
    second = store.create_mapping("legacy-second@icloud.com", category=source_tag.name)
    client = _client(tmp_path, store)

    claimed = client.post(
        "/api/workbench/claim-next",
        data={"category": source_tag.name, "target_tag_id": platform_tag.id},
    ).json()["mapping"]
    assert claimed["id"] == first.id
    _clear_claim_platform(store, first.id)

    response = client.post(
        "/api/workbench/claim-next",
        data={
            "category": source_tag.name,
            "target_tag_id": platform_tag.id,
            "address_mode": "icloud_alias",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["mapping"]["address_kind"] == "icloud_alias"
    assert payload["mapping"]["parent_mapping_id"] == second.id
    assert payload["mapping"]["target_site"] == platform_tag.name
    assert store.get_by_id(first.id).status == "idle"
