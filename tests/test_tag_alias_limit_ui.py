from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


def _admin_client(tmp_path) -> tuple[TestClient, KeyStore]:
    store = KeyStore(tmp_path / "app.db")
    settings = AppSettings(
        app_secret_key="tag-alias-limit-ui-test",
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
    return client, store


def test_admin_can_configure_per_tag_alias_use_limit(tmp_path) -> None:
    client, store = _admin_client(tmp_path)

    created = client.post(
        "/admin/tags",
        data={
            "name": "GPT",
            "kind": "service",
            "alias_use_limit": "5",
        },
        follow_redirects=True,
    )
    tag_id = store.get_category_id("GPT")
    tag = store.get_tag(tag_id)

    assert created.status_code == 200
    assert tag is not None and tag.alias_use_limit == 5
    assert "单邮箱裂变接码上限" in created.text
    assert "裂变上限 5 次/邮箱" in created.text
    assert 'data-alias-use-limit="5"' in created.text
    assert "内置接码规则" in created.text
    assert "当前生效（内置）：发件人 @openai.com、*@*.openai.com。" in created.text

    updated = client.post(
        f"/admin/tags/{tag.id}/update",
        data={
            "name": "GPT",
            "kind": "service",
            "alias_use_limit": "3",
        },
        follow_redirects=True,
    )

    assert updated.status_code == 200
    assert store.get_tag(tag.id).alias_use_limit == 3
    assert "裂变上限 3 次/邮箱" in updated.text


def test_admin_rejects_negative_alias_use_limit_with_clear_error(tmp_path) -> None:
    client, store = _admin_client(tmp_path)

    response = client.post(
        "/admin/tags",
        data={
            "name": "Invalid",
            "kind": "service",
            "alias_use_limit": "-1",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "裂变接码上限必须是 0 或正整数" in response.text
    assert store.get_category_id("Invalid") is None
