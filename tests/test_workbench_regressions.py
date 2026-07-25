from __future__ import annotations

from fastapi.testclient import TestClient

from app.cloudmail import CloudMailMessage
from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


class WorkbenchMailboxClient:
    def __init__(self) -> None:
        self.messages: list[CloudMailMessage] = []

    def fetch_recent_emails(self, recipient_email: str, limit: int = 10):
        return self.messages[:limit]


def _logged_in_client(
    tmp_path,
    store: KeyStore,
    cloudmail: WorkbenchMailboxClient,
) -> TestClient:
    settings = AppSettings(
        app_secret_key="workbench-regression-test",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_api_token="token",
    )
    client = TestClient(create_app(settings=settings, store=store, cloudmail_client=cloudmail))
    login = client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    return client


def _clear_active_target_site(store: KeyStore, mapping_id: int) -> None:
    """模拟升级前已经领取、但尚未保存平台标签的历史记录。"""

    with store._connect() as connection:
        connection.execute(
            "UPDATE access_mappings SET target_site = '' WHERE id = ?",
            (int(mapping_id),),
        )
        connection.commit()


def test_workbench_source_and_platform_selects_are_top_aligned(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    store.create_tag("Chatgpt", kind="service")
    client = _logged_in_client(tmp_path, store, WorkbenchMailboxClient())

    response = client.get("/admin/workbench")

    assert response.status_code == 200
    assert 'class="mt-5 grid gap-4 lg:grid-cols-4 lg:items-start"' in response.text
    assert "lg:grid-cols-4 lg:items-end" not in response.text


def test_legacy_claim_without_platform_must_be_finished_before_alias_mode(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    platform = store.create_tag("Chatgpt", kind="service")
    first = store.create_mapping("legacy-first@icloud.com", category="未使用")
    second = store.create_mapping("legacy-second@icloud.com", category="未使用")
    client = _logged_in_client(tmp_path, store, WorkbenchMailboxClient())

    claimed = client.post(
        "/api/workbench/claim-next",
        data={
            "category": "未使用",
            "target_tag_id": str(platform.id),
            "address_mode": "primary",
        },
    )
    assert claimed.status_code == 200
    assert claimed.json()["mapping"]["id"] == first.id
    _clear_active_target_site(store, first.id)

    alias_response = client.post(
        "/api/workbench/claim-next",
        data={
            "category": "未使用",
            "target_tag_id": str(platform.id),
            "address_mode": "icloud_alias",
        },
    )
    assert alias_response.status_code == 409
    assert "请先确认当前邮箱已接码" in alias_response.json()["message"]
    rebound = store.get_by_id(first.id)
    assert rebound.status == "in_progress"
    assert rebound.target_site == platform.name
    assert store.get_by_id(second.id).status == "idle"


def test_legacy_claim_without_platform_can_be_skipped(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    platform = store.create_tag("Chatgpt", kind="service")
    mapping = store.create_mapping("legacy-skip@icloud.com", category="未使用")
    client = _logged_in_client(tmp_path, store, WorkbenchMailboxClient())

    claimed = client.post(
        "/api/workbench/claim-next",
        data={"category": "未使用", "target_tag_id": str(platform.id)},
    ).json()["mapping"]
    _clear_active_target_site(store, mapping.id)

    skipped = client.post(
        "/api/workbench/current/skip",
        data={
            "mapping_id": str(claimed["id"]),
            "category": "未使用",
            "target_tag_id": str(platform.id),
        },
    )

    assert skipped.status_code == 200
    assert "其他接码平台" not in skipped.json().get("message", "")
    assert skipped.json()["mapping"] is None
    assert store.get_by_id(mapping.id).status == "idle"


def test_refresh_binds_selected_platform_for_legacy_claim(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    platform = store.create_tag("Chatgpt", kind="service")
    mapping = store.create_mapping("legacy-refresh@icloud.com", category="未使用")
    cloudmail = WorkbenchMailboxClient()
    client = _logged_in_client(tmp_path, store, cloudmail)

    claimed = client.post(
        "/api/workbench/claim-next",
        data={"category": "未使用", "target_tag_id": str(platform.id)},
    )
    assert claimed.status_code == 200
    _clear_active_target_site(store, mapping.id)
    cloudmail.messages = [
        CloudMailMessage(
            email_id=901,
            send_email="noreply@tm.openai.com",
            send_name="OpenAI",
            subject="Your verification code is 901901",
            to_email=mapping.recipient_email,
            to_name="",
            create_time="2099-01-01 00:00:00",
            type=0,
            content="<p>901901</p>",
            text="Your verification code is 901901",
            is_del=0,
        )
    ]

    refreshed = client.get(
        "/api/workbench/current/mailbox",
        params={"category": "未使用", "target_tag_id": platform.id},
    )

    assert refreshed.status_code == 200
    assert refreshed.json()["latest_code"] == "901901"
    assert store.get_by_id(mapping.id).target_site == platform.name

    page = client.get("/admin/workbench")
    assert "const targetTagId = targetTagInput?.value || '';" in page.text
    assert "`&target_tag_id=${encodeURIComponent(targetTagId)}`" in page.text
    assert "${targetTagQuery}`" in page.text
