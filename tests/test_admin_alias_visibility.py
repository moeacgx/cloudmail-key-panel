from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


def test_admin_account_list_hides_temporary_icloud_aliases(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source_tag = store.create_tag("未使用", kind="business")
    root = store.create_mapping("inventory@icloud.com", access_key="root-key")
    store.set_mapping_tags(root.id, [source_tag.id])
    alias = store.create_icloud_alias(root.id, alias_tag="temporary", access_key="alias-key")

    # 裂变地址仍作为领取与验证码流水的内部记录存在，但不属于后台邮箱库存。
    assert store.count_mappings() == 2
    assert store.count_mappings(include_aliases=False) == 1
    assert [item.id for item in store.list_mappings(include_aliases=False)] == [root.id]
    assert store.count_mappings(
        search_query=alias.recipient_email,
        include_aliases=False,
    ) == 0
    assert [
        item.id
        for item in store.list_mappings(
            category_filter=source_tag.name,
            include_aliases=False,
        )
    ] == [root.id]
    assert [item.id for item in store.list_icloud_aliases(root.id)] == [alias.id]
    assert store.get_by_key("alias-key").id == alias.id

    settings = AppSettings(
        app_secret_key="alias-visibility-test",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_api_token="token",
    )
    client = TestClient(create_app(settings=settings, store=store))
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
    )

    response = client.get("/admin")

    assert response.status_code == 200
    assert root.recipient_email in response.text
    assert alias.recipient_email not in response.text
    assert "当前共 1 个 Key" in response.text
