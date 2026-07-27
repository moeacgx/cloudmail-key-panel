from __future__ import annotations

from fastapi.testclient import TestClient

from app.cloudmail import CloudMailError, CloudMailMessage
from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


class MutableMailboxClient:
    def __init__(self) -> None:
        self.error = ""
        self.messages: list[CloudMailMessage] = []

    def set_code(self, recipient: str, code: str, *, email_id: int = 1) -> None:
        self.messages = [
            CloudMailMessage(
                email_id=email_id,
                send_email="noreply@openai.com",
                send_name="OpenAI",
                subject=f"Your verification code is {code}",
                to_email=recipient,
                to_name="",
                create_time="2099-01-01 00:00:00",
                type=0,
                content=f"<p>{code}</p>",
                text=f"Your verification code is {code}",
                is_del=0,
            )
        ]

    def fetch_recent_emails(self, recipient_email: str, limit: int = 10):
        if self.error:
            raise CloudMailError(self.error)
        return self.messages[:limit]


def _admin_client(tmp_path, store: KeyStore, cloudmail: MutableMailboxClient) -> TestClient:
    settings = AppSettings(
        app_secret_key="workbench-regression-test",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_api_token="token",
    )
    client = TestClient(create_app(settings=settings, store=store, cloudmail_client=cloudmail))
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})
    return client


def _claim(client: TestClient, *, category: str, target_tag_id: int):
    response = client.post(
        "/api/workbench/claim-next",
        data={"category": category, "target_tag_id": str(target_tag_id)},
    )
    assert response.status_code == 200
    return response.json()["mapping"]


def test_skip_button_does_not_require_a_platform_selection(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    client = _admin_client(tmp_path, store, MutableMailboxClient())

    page = client.get("/admin/workbench")

    assert page.status_code == 200
    assert "if (isCompletion && !targetTagInput?.value)" in page.text
    assert "已有备注 / 标签" in page.text
    assert "data-current-platform" in page.text
    assert "本次平台：${mapping.target_site}" in page.text


def test_skip_releases_current_even_when_mailbox_query_is_broken(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source = store.create_tag("未使用", kind="business")
    platform = store.create_tag("OpenAI", kind="service")
    mapping = store.create_mapping("skip-on-error@example.com", category=source.name)
    cloudmail = MutableMailboxClient()
    client = _admin_client(tmp_path, store, cloudmail)
    claimed = _claim(client, category=source.name, target_tag_id=platform.id)
    assert claimed["id"] == mapping.id

    cloudmail.error = "upstream unavailable"
    skipped = client.post(
        "/api/workbench/current/skip",
        data={"mapping_id": str(mapping.id)},
    )

    assert skipped.status_code == 200
    assert skipped.json()["mapping"] is None
    assert "解除占用" in skipped.json()["message"]
    refreshed = store.get_by_id(mapping.id)
    assert refreshed is not None and refreshed.status == "idle"
    assert platform.id not in {tag.id for tag in store.list_mapping_tags(mapping.id)}
    assert store.count_verification_events(tag_id=platform.id) == 0


def test_void_current_retires_mailbox_and_replaces_source_tag(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source = store.create_tag("待注册", kind="business")
    platform = store.create_tag("OpenAI", kind="service")
    history = store.create_tag("Claude", kind="service")
    mapping = store.create_mapping("dead-mailbox@example.com", category=source.name)
    store.add_mapping_tag(mapping.id, history.id, source="usage")
    cloudmail = MutableMailboxClient()
    client = _admin_client(tmp_path, store, cloudmail)
    claimed = _claim(client, category=source.name, target_tag_id=platform.id)
    assert claimed["id"] == mapping.id

    voided = client.post(
        "/api/workbench/current/void",
        data={"mapping_id": str(mapping.id)},
    )

    assert voided.status_code == 200
    assert voided.json()["mapping"] is None
    assert "不会再次发放" in voided.json()["message"]
    refreshed = store.get_by_id(mapping.id)
    assert refreshed is not None
    assert refreshed.status == "idle"
    assert refreshed.reuse_policy == "retired"
    assert refreshed.target_site == ""
    tags = {tag.name: tag for tag in store.list_mapping_tags(mapping.id)}
    assert "待注册" not in tags
    assert "OpenAI" not in tags
    assert tags["邮箱作废"].prevents_reuse is True
    assert tags["Claude"].kind == "service"
    assert store.count_verification_events(tag_id=platform.id) == 0

    repeated = client.post(
        "/api/workbench/claim-next",
        data={"category": "", "target_tag_id": str(platform.id)},
    )
    assert repeated.status_code == 200
    assert repeated.json()["mapping"] is None

    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "已作废" in dashboard.text
    assert "完全未使用" not in dashboard.text


def test_workbench_page_exposes_confirmed_void_action(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    client = _admin_client(tmp_path, store, MutableMailboxClient())

    page = client.get("/admin/workbench")

    assert page.status_code == 200
    assert "作废当前邮箱" in page.text
    assert "/api/workbench/current/void" in page.text
    assert "window.confirm" in page.text


def test_current_mapping_can_switch_between_all_platform_tags(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source = store.create_tag("未使用", kind="business")
    openai = store.create_tag("OpenAI", kind="service", sender_patterns="@openai.com")
    grok = store.create_tag("Grok", kind="service", sender_patterns="@openai.com")
    claude = store.create_tag("Claude", kind="service", sender_patterns="@openai.com")
    mapping = store.create_mapping("switch-platform@example.com", category=source.name)
    cloudmail = MutableMailboxClient()
    client = _admin_client(tmp_path, store, cloudmail)
    claimed = _claim(client, category=source.name, target_tag_id=claude.id)
    assert claimed["id"] == mapping.id

    page = client.get("/admin/workbench")
    assert page.status_code == 200
    for tag in (openai, grok, claude):
        assert f'value="{tag.id}"' in page.text
    assert "targetTagInput.disabled = true" not in page.text

    cloudmail.set_code(mapping.recipient_email, "123456", email_id=10)
    old_platform_mailbox = client.get(
        "/api/workbench/current/mailbox",
        params={"target_tag_id": claude.id},
    )
    assert old_platform_mailbox.json()["latest_code"] == "123456"

    switched_to_grok = client.get(
        "/api/workbench/current/mailbox",
        params={"target_tag_id": grok.id},
    )
    assert switched_to_grok.status_code == 200
    assert switched_to_grok.json()["mapping"]["target_site"] == "Grok"
    assert switched_to_grok.json()["latest_code"] is None
    assert store.get_by_id(mapping.id).last_seen_email_id == 10

    cloudmail.set_code(mapping.recipient_email, "234567", email_id=11)
    new_platform_mailbox = client.get(
        "/api/workbench/current/mailbox",
        params={"target_tag_id": grok.id},
    )
    assert new_platform_mailbox.json()["latest_code"] == "234567"

    switched_to_openai = client.get(
        "/api/workbench/current/mailbox",
        params={"target_tag_id": openai.id},
    )
    assert switched_to_openai.status_code == 200
    assert switched_to_openai.json()["mapping"]["target_site"] == "OpenAI"


def test_current_mapping_cannot_switch_to_an_already_used_platform(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source = store.create_tag("待注册", kind="business")
    openai = store.create_tag("OpenAI", kind="service")
    claude = store.create_tag("Claude", kind="service")
    mapping = store.create_mapping("used-platform@example.com", category=source.name)
    store.add_mapping_tag(mapping.id, openai.id, source="usage")
    client = _admin_client(tmp_path, store, MutableMailboxClient())
    claimed = _claim(client, category=source.name, target_tag_id=claude.id)
    assert claimed["id"] == mapping.id

    rejected = client.get(
        "/api/workbench/current/mailbox",
        params={"target_tag_id": openai.id},
    )

    assert rejected.status_code == 409
    assert "已经用于该平台" in rejected.json()["message"]
    assert store.get_by_id(mapping.id).target_site == "Claude"


def test_icloud_alias_must_be_reclaimed_to_change_platform(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source = store.create_tag("未使用", kind="business")
    openai = store.create_tag("OpenAI", kind="service")
    grok = store.create_tag("Grok", kind="service")
    store.create_mapping("alias-switch@icloud.com", category=source.name)
    client = _admin_client(tmp_path, store, MutableMailboxClient())
    claimed = client.post(
        "/api/workbench/claim-next",
        data={
            "category": source.name,
            "target_tag_id": str(openai.id),
            "address_mode": "icloud_alias",
        },
    )
    assert claimed.status_code == 200
    alias_mapping = claimed.json()["mapping"]
    assert alias_mapping["address_kind"] == "icloud_alias"

    rejected = client.get(
        "/api/workbench/current/mailbox",
        params={"target_tag_id": grok.id},
    )

    assert rejected.status_code == 409
    assert "裂变邮箱领取后不能切换平台" in rejected.json()["message"]
    assert store.get_by_id(alias_mapping["id"]).target_site == "OpenAI"


def test_failed_initial_snapshot_still_restores_mapping_and_allows_skip(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source = store.create_tag("未使用", kind="business")
    platform = store.create_tag("OpenAI", kind="service")
    mapping = store.create_mapping("snapshot-error@example.com", category=source.name)
    cloudmail = MutableMailboxClient()
    cloudmail.error = "upstream unavailable"
    client = _admin_client(tmp_path, store, cloudmail)

    failed_claim = client.post(
        "/api/workbench/claim-next",
        data={"category": source.name, "target_tag_id": str(platform.id)},
    )
    restored = client.get("/api/workbench/current")

    assert failed_claim.status_code == 502
    assert restored.status_code == 200
    assert restored.json()["mapping"]["id"] == mapping.id
    assert "仍可跳过当前邮箱" in restored.json()["message"]

    skipped = client.post(
        "/api/workbench/current/skip",
        data={"mapping_id": str(mapping.id)},
    )
    assert skipped.status_code == 200
    assert store.get_by_id(mapping.id).status == "idle"


def test_dashboard_can_save_manual_tags_and_release_occupied_mapping_on_mailbox_error(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source = store.create_tag("未使用", kind="business")
    platform = store.create_tag("OpenAI", kind="service")
    mapping = store.create_mapping(
        "save-tag-on-error@example.com",
        query_email="save-tag-on-error@example.com",
        access_key="save-tag-on-error-key",
        category=source.name,
    )
    cloudmail = MutableMailboxClient()
    client = _admin_client(tmp_path, store, cloudmail)
    _claim(client, category=source.name, target_tag_id=platform.id)

    cloudmail.error = "upstream unavailable"
    saved = client.post(
        f"/admin/keys/{mapping.id}/update",
        data={
            "recipient_email": mapping.recipient_email,
            "query_email": mapping.query_email,
            "access_key": mapping.access_key,
            "category_value": source.name,
            "tag_ids": [str(source.id), str(platform.id)],
        },
    )

    assert saved.status_code == 200
    assert "工作台占用已自动解除" in saved.text
    refreshed = store.get_by_id(mapping.id)
    assert refreshed is not None and refreshed.status == "idle"
    assert {tag.id for tag in store.list_mapping_tags(mapping.id)} == {platform.id}


def test_claim_next_refuses_to_replace_an_unfinished_mapping(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source = store.create_tag("未使用", kind="business")
    platform = store.create_tag("OpenAI", kind="service")
    first = store.create_mapping("first-unfinished@example.com", category=source.name)
    second = store.create_mapping("second-unfinished@example.com", category=source.name)
    cloudmail = MutableMailboxClient()
    client = _admin_client(tmp_path, store, cloudmail)
    claimed = _claim(client, category=source.name, target_tag_id=platform.id)
    assert claimed["id"] == first.id

    repeated = client.post(
        "/api/workbench/claim-next",
        data={"category": source.name, "target_tag_id": str(platform.id)},
    )

    assert repeated.status_code == 409
    assert "请先确认当前邮箱已接码" in repeated.json()["message"]
    assert store.get_by_id(first.id).status == "in_progress"
    assert store.get_by_id(second.id).status == "idle"
    assert store.count_verification_events(tag_id=platform.id) == 0


def test_confirming_code_finishes_current_without_automatically_claiming_next(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source = store.create_tag("未使用", kind="business")
    platform = store.create_tag("OpenAI", kind="service")
    first = store.create_mapping("first-completed@example.com", category=source.name)
    second = store.create_mapping("second-after-completed@example.com", category=source.name)
    cloudmail = MutableMailboxClient()
    client = _admin_client(tmp_path, store, cloudmail)
    claimed = _claim(client, category=source.name, target_tag_id=platform.id)
    assert claimed["id"] == first.id

    cloudmail.set_code(first.recipient_email, "345678")
    completed = client.post(
        "/api/workbench/current/mark-used",
        data={"mapping_id": str(first.id), "target_tag_id": str(platform.id)},
    )

    assert completed.status_code == 200
    assert completed.json()["mapping"] is None
    assert "可以领取下一个邮箱" in completed.json()["message"]
    assert store.get_by_id(first.id).status == "idle"
    assert store.get_by_id(second.id).status == "idle"
    assert platform.id in {tag.id for tag in store.list_mapping_tags(first.id)}
    assert store.count_verification_events(tag_id=platform.id) == 1

    next_claim = _claim(client, category=source.name, target_tag_id=platform.id)
    assert next_claim["id"] == second.id
