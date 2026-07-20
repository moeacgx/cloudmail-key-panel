from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from app.cloudmail import CloudMailError, CloudMailMessage
from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


class FakeCloudMailClient:
    def __init__(
        self,
        messages: list[CloudMailMessage] | None = None,
        error: str | None = None,
    ) -> None:
        self.messages = messages or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def fetch_recent_emails(self, recipient_email: str, limit: int = 10) -> list[CloudMailMessage]:
        self.calls.append((recipient_email, limit))
        if self.error:
            raise CloudMailError(self.error)
        return self.messages[:limit]


def _settings(tmp_path) -> AppSettings:
    return AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )


def _headers(client_id: str = "worker-a") -> dict[str, str]:
    return {"X-Client-ID": client_id}


def test_external_api_requires_basic_auth_and_lists_category_ids(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping(recipient_email="first@example.com", category="ChatGPT")
    store.create_mapping(recipient_email="second@example.com", category="ChatGPT")
    store.create_mapping(recipient_email="used@example.com", category="gpt废号")
    client = TestClient(
        create_app(settings=_settings(tmp_path), store=store, cloudmail_client=FakeCloudMailClient())
    )

    missing_auth = client.get("/api/v1/categories")
    wrong_auth = client.get("/api/v1/categories", auth=("admin", "wrong"))
    missing_auth_and_body = client.post("/api/v1/workbench/claim-next")
    unicode_credentials = base64.b64encode("管理员:wrong".encode("utf-8")).decode("ascii")
    unicode_auth = client.get(
        "/api/v1/categories",
        headers={"Authorization": f"Basic {unicode_credentials}"},
    )
    response = client.get("/api/v1/categories", auth=("admin", "pass123"))
    chatgpt_tag_id = store.get_category_id("ChatGPT")
    assert chatgpt_tag_id is not None
    store.set_tag_alias_use_limit(chatgpt_tag_id, 5)
    tags_response = client.get("/api/v1/tags", auth=("admin", "pass123"))

    assert missing_auth.status_code == 401
    assert missing_auth.headers["www-authenticate"].startswith("Basic ")
    assert missing_auth.json()["error"]["code"] == "unauthorized"
    assert wrong_auth.status_code == 401
    assert missing_auth_and_body.status_code == 401
    assert missing_auth_and_body.headers["cache-control"] == "no-store"
    assert unicode_auth.status_code == 401
    assert response.status_code == 200
    assert response.json()["categories"] == [
        {
            "id": store.get_category_id("ChatGPT"),
            "name": "ChatGPT",
            "count": 2,
        },
        {
            "id": store.get_category_id("gpt废号"),
            "name": "gpt废号",
            "count": 1,
        },
    ]
    assert tags_response.status_code == 200
    chatgpt_tag = next(tag for tag in tags_response.json()["tags"] if tag["name"] == "ChatGPT")
    assert chatgpt_tag["alias_use_limit"] == 5
    assert chatgpt_tag["prevent_reuse"] is False


def test_external_api_claim_is_idempotent_and_isolated_by_client_id(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source_tag = store.create_tag("未使用", kind="business")
    target_tag = store.create_tag("GPT", kind="service")
    first = store.create_mapping(
        recipient_email="first@example.com",
        query_email="shared@example.com",
        access_key="private-first-key",
        category=source_tag.name,
    )
    second = store.create_mapping(
        recipient_email="second@example.com",
        access_key="private-second-key",
        category=source_tag.name,
    )
    store.create_tag("Other", kind="business")
    other = store.create_mapping(recipient_email="other@example.com", category="Other")
    client = TestClient(
        create_app(settings=_settings(tmp_path), store=store, cloudmail_client=FakeCloudMailClient())
    )
    category_id = source_tag.id

    first_claim = client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": category_id, "target_tag_id": target_tag.id},
        headers=_headers("worker-a"),
        auth=("admin", "pass123"),
    )
    retry_claim = client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": category_id, "target_tag_id": target_tag.id},
        headers=_headers("worker-a"),
        auth=("admin", "pass123"),
    )
    second_claim = client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": category_id, "target_tag_id": target_tag.id},
        headers=_headers("worker-b"),
        auth=("admin", "pass123"),
    )

    assert first_claim.status_code == 200
    assert first_claim.json()["mapping"]["id"] == first.id
    assert first_claim.json()["registration_email"] == "first@example.com"
    assert retry_claim.status_code == 200
    assert retry_claim.json()["mapping"]["id"] == first.id
    assert retry_claim.json()["message"] == "已恢复当前领取的邮箱"
    assert second_claim.status_code == 200
    assert second_claim.json()["mapping"]["id"] == second.id
    assert store.get_by_id(first.id).claimed_by == "api:worker-a"
    assert store.get_by_id(second.id).claimed_by == "api:worker-b"
    assert store.get_by_id(other.id).status == "idle"

    serialized = json.dumps(first_claim.json(), ensure_ascii=False)
    assert "private-first-key" not in serialized
    assert "shared@example.com" not in serialized
    assert "access_key" not in serialized
    assert "query_email" not in serialized


def test_external_api_keeps_selected_source_tag_for_multitag_mapping(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    legacy_tag = store.create_tag("旧库存", kind="business")
    selected_tag = store.create_tag("新库存", kind="business")
    target_tag = store.create_tag("GPT", kind="service")
    mapping = store.create_mapping("multi-tag@example.com", category=legacy_tag.name)
    store.add_mapping_tag(mapping.id, selected_tag.id)
    client = TestClient(
        create_app(settings=_settings(tmp_path), store=store, cloudmail_client=FakeCloudMailClient())
    )

    claimed = client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": selected_tag.id, "target_tag_id": target_tag.id},
        headers=_headers("multi-tag-worker"),
        auth=("admin", "pass123"),
    )
    current = client.get(
        "/api/v1/workbench/current",
        headers=_headers("multi-tag-worker"),
        auth=("admin", "pass123"),
    )

    assert claimed.status_code == 200
    assert claimed.json()["mapping"]["category_id"] == selected_tag.id
    assert claimed.json()["mapping"]["category"] == "新库存"
    assert current.status_code == 200
    assert current.json()["mapping"]["category_id"] == selected_tag.id

    missing_client_id = client.get(
        "/api/v1/workbench/current",
        auth=("admin", "pass123"),
    )
    invalid_client_id = client.get(
        "/api/v1/workbench/current",
        headers=_headers("worker a"),
        auth=("admin", "pass123"),
    )
    assert missing_client_id.status_code == 400
    assert missing_client_id.json()["error"]["code"] == "client_id_required"
    assert invalid_client_id.status_code == 400
    assert invalid_client_id.json()["error"]["code"] == "client_id_invalid"


def test_external_api_claim_snapshots_preexisting_mail_before_returning_mapping(
    tmp_path,
    monkeypatch,
) -> None:
    """外部 API 与后台工作台使用相同的领取时邮件快照口径。"""

    monkeypatch.setattr(KeyStore, "_now", staticmethod(lambda: "2026-07-18 10:00:00"))
    store = KeyStore(tmp_path / "app.db")
    source_tag = store.create_tag("未使用", kind="business")
    target_tag = store.create_tag("GPT", kind="service")
    mapping = store.create_mapping("api-snapshot@icloud.com", category=source_tag.name)
    old_message = CloudMailMessage(
        email_id=301,
        send_email="noreply@tm.openai.com",
        send_name="OpenAI",
        subject="Your verification code is 301301",
        to_email=mapping.recipient_email,
        to_name="",
        create_time="2026-07-18 10:00:00",
        type=0,
        content="<p>301301</p>",
        text="Your verification code is 301301",
        is_del=0,
    )
    cloudmail = FakeCloudMailClient([old_message])
    client = TestClient(create_app(settings=_settings(tmp_path), store=store, cloudmail_client=cloudmail))
    auth = ("admin", "pass123")
    headers = _headers("snapshot-worker")

    claimed = client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": source_tag.id, "target_tag_id": target_tag.id},
        headers=headers,
        auth=auth,
    )

    assert claimed.status_code == 200
    assert claimed.json()["mapping"]["id"] == mapping.id
    assert claimed.json()["latest_code"] is None
    assert store.get_by_id(mapping.id).last_seen_email_id == 301
    assert len(cloudmail.calls) == 1

    cloudmail.messages.insert(
        0,
        CloudMailMessage(
            email_id=302,
            send_email="noreply@tm.openai.com",
            send_name="OpenAI",
            subject="Your verification code is 302302",
            to_email=mapping.recipient_email,
            to_name="",
            create_time="2026-07-18 10:00:00",
            type=0,
            content="<p>302302</p>",
            text="Your verification code is 302302",
            is_del=0,
        ),
    )
    current = client.get(
        "/api/v1/workbench/current",
        headers=headers,
        auth=auth,
    )

    assert current.status_code == 200
    assert current.json()["latest_code"] == "302302"
    assert current.json()["latest_email"]["email_id"] == 302
    assert len(cloudmail.calls) == 2


def test_external_api_returns_latest_code_and_complete_latest_email_without_cross_mailbox_leak(tmp_path) -> None:
    target_email = CloudMailMessage(
        email_id=20,
        send_email="welcome@example.com",
        send_name="Welcome",
        subject="欢迎注册",
        to_email="openai@eve.ink",
        to_name="Shared inbox",
        create_time="2099-01-01 00:02:00",
        type=0,
        content="<div>完整 HTML 正文</div><div>收件人：target@example.com</div>",
        text="完整纯文本正文\n收件人：target@example.com",
        is_del=0,
        recipient='[{"address":"target@example.com","name":"Target"}]',
    )
    target_code_email = CloudMailMessage(
        email_id=19,
        send_email="noreply@tm.openai.com",
        send_name="OpenAI",
        subject="你的验证码是 330119",
        to_email="openai@eve.ink",
        to_name="Shared inbox",
        create_time="2099-01-01 00:01:00",
        type=0,
        content="<div>验证码 330119</div><div>收件人：target@example.com</div>",
        text="验证码 330119\n收件人：target@example.com",
        is_del=0,
        recipient='[{"address":"target@example.com"}]',
    )
    other_email = CloudMailMessage(
        email_id=21,
        send_email="noreply@tm.openai.com",
        send_name="OpenAI",
        subject="其他人的验证码 999999",
        to_email="openai@eve.ink",
        to_name="Shared inbox",
        create_time="2099-01-01 00:03:00",
        type=0,
        content="<div>验证码 999999</div><div>收件人：other@example.com</div>",
        text="验证码 999999\n收件人：other@example.com",
        is_del=0,
        recipient='[{"address":"other@example.com"}]',
    )
    fake_cloudmail = FakeCloudMailClient()
    store = KeyStore(tmp_path / "app.db")
    source_tag = store.create_tag("未使用", kind="business")
    target_tag = store.create_tag("GPT", kind="service")
    mapping = store.create_mapping(
        recipient_email="target@example.com",
        query_email="openai@eve.ink",
        category=source_tag.name,
    )
    client = TestClient(create_app(settings=_settings(tmp_path), store=store, cloudmail_client=fake_cloudmail))

    response = client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": source_tag.id, "target_tag_id": target_tag.id},
        headers=_headers(),
        auth=("admin", "pass123"),
    )
    claim_payload = response.json()

    assert response.status_code == 200
    assert claim_payload["mapping"]["id"] == mapping.id
    assert claim_payload["registration_email"] == "target@example.com"
    assert claim_payload["latest_code"] is None

    # 邮箱交付后才注入邮件，验证查码阶段仍会过滤其他收件人。
    fake_cloudmail.messages = [other_email, target_email, target_code_email]
    current = client.get(
        "/api/v1/workbench/current",
        headers=_headers(),
        auth=("admin", "pass123"),
    )
    payload = current.json()

    assert current.status_code == 200
    assert payload["latest_code"] == "330119"
    assert payload["latest_email"]["email_id"] == 19
    assert payload["latest_email"]["send_email"] == "noreply@tm.openai.com"
    assert payload["latest_email"]["codes"] == ["330119"]
    assert payload["latest_email"]["detected_recipients"] == ["target@example.com"]
    assert "999999" not in json.dumps(payload, ensure_ascii=False)
    assert fake_cloudmail.calls == [("openai@eve.ink", 5)] * 2


def test_external_api_complete_and_skip_follow_workbench_category_flow(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source_tag = store.create_tag("未使用", kind="business")
    skip_source_tag = store.create_tag("待跳过", kind="business")
    target_tag = store.create_tag("GPT", kind="service")
    first = store.create_mapping(recipient_email="first@example.com", category=source_tag.name)
    second = store.create_mapping(recipient_email="second@example.com", category=source_tag.name)
    skip_first = store.create_mapping(recipient_email="skip-first@example.com", category=skip_source_tag.name)
    skip_second = store.create_mapping(recipient_email="skip-second@example.com", category=skip_source_tag.name)
    verification_message = CloudMailMessage(
        email_id=1,
        send_email="noreply@tm.openai.com",
        send_name="OpenAI",
        subject="Your verification code is 112233",
        to_email="first@example.com",
        to_name="",
        create_time="2099-01-01 00:00:00",
        type=0,
        content="<p>112233</p>",
        text="Your verification code is 112233",
        is_del=0,
    )
    fake_cloudmail = FakeCloudMailClient()
    client = TestClient(
        create_app(
            settings=_settings(tmp_path),
            store=store,
            cloudmail_client=fake_cloudmail,
        )
    )
    source_category_id = source_tag.id

    claim = client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": source_category_id, "target_tag_id": target_tag.id},
        headers=_headers("complete-worker"),
        auth=("admin", "pass123"),
    ).json()
    fake_cloudmail.messages = [verification_message]
    response = client.post(
        "/api/v1/workbench/complete",
        json={
            "mapping_id": claim["mapping"]["id"],
            "category_id": source_category_id,
        },
        headers=_headers("complete-worker"),
        auth=("admin", "pass123"),
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["completed"]["id"] == first.id
    assert payload["completed"]["category"] == source_tag.name
    assert payload["mapping"]["id"] == second.id
    assert store.get_by_id(first.id).category == source_tag.name
    assert store.get_by_id(first.id).status == "idle"
    assert store.get_by_id(first.id).target_site == target_tag.name
    assert store.get_by_id(second.id).claimed_by == "api:complete-worker"
    assert store.get_by_id(second.id).target_site == target_tag.name
    assert store.count_verification_events(tag_id=target_tag.id) == 1

    skip_category_id = skip_source_tag.id
    skip_claim = client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": skip_category_id, "target_tag_id": target_tag.id},
        headers=_headers("skip-worker"),
        auth=("admin", "pass123"),
    ).json()
    skip_response = client.post(
        "/api/v1/workbench/skip-current",
        json={
            "mapping_id": skip_claim["mapping"]["id"],
            "category_id": skip_category_id,
        },
        headers=_headers("skip-worker"),
        auth=("admin", "pass123"),
    )
    skip_payload = skip_response.json()

    assert skip_response.status_code == 200
    assert skip_payload["skipped"]["id"] == skip_first.id
    assert skip_payload["skipped"]["category"] == "待跳过"
    assert skip_payload["mapping"]["id"] == skip_second.id
    assert store.get_by_id(skip_first.id).status == "idle"
    assert store.get_by_id(skip_first.id).category == "待跳过"


def test_external_api_skip_records_arrived_code_as_success(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    first = store.create_mapping(recipient_email="api-skip-code-first@example.com", category="未使用")
    second = store.create_mapping(recipient_email="api-skip-code-second@example.com", category="未使用")
    gpt = store.create_tag("GPT", kind="service")
    message = CloudMailMessage(
        email_id=31,
        send_email="noreply@tm.openai.com",
        send_name="OpenAI",
        subject="Your verification code is 554433",
        to_email=first.recipient_email,
        to_name="",
        create_time="2099-01-01 00:00:00",
        type=0,
        content="<p>554433</p>",
        text="Your verification code is 554433",
        is_del=0,
    )
    fake_cloudmail = FakeCloudMailClient()
    client = TestClient(
        create_app(
            settings=_settings(tmp_path),
            store=store,
            cloudmail_client=fake_cloudmail,
        )
    )
    source_id = store.get_category_id("未使用")
    headers = _headers("skip-code-worker")
    claimed = client.post(
        "/api/v1/workbench/claim-next",
        json={
            "category_id": source_id,
            "target_tag_id": gpt.id,
        },
        headers=headers,
        auth=("admin", "pass123"),
    ).json()["mapping"]
    fake_cloudmail.messages = [message]

    response = client.post(
        "/api/v1/workbench/skip-current",
        json={
            "mapping_id": claimed["id"],
            "category_id": source_id,
        },
        headers=headers,
        auth=("admin", "pass123"),
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["skipped"] is None
    assert payload["completed"]["id"] == first.id
    assert payload["mapping"]["id"] == second.id
    assert "已按成功接码记录" in payload["message"]
    assert store.get_by_id(first.id).first_used_at
    assert [tag.id for tag in store.list_mapping_tags(first.id) if tag.name == "GPT"] == [gpt.id]
    assert store.count_verification_events(tag_id=gpt.id) == 1


def test_external_api_hides_uninitialized_claim_when_cloudmail_query_fails(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source_tag = store.create_tag("未使用", kind="business")
    target_tag = store.create_tag("GPT", kind="service")
    mapping = store.create_mapping(recipient_email="first@example.com", category=source_tag.name)
    fake_cloudmail = FakeCloudMailClient(error="upstream unavailable")
    client = TestClient(
        create_app(
            settings=_settings(tmp_path),
            store=store,
            cloudmail_client=fake_cloudmail,
        )
    )

    response = client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": source_tag.id, "target_tag_id": target_tag.id},
        headers=_headers("failure-worker"),
        auth=("admin", "pass123"),
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "cloudmail_error"
    assert response.json()["mapping"] is None
    assert store.get_by_id(mapping.id).status == "in_progress"
    assert store.get_by_id(mapping.id).claimed_by == "api:failure-worker"
    assert not store.is_workbench_claim_baseline_ready(
        mapping.id,
        claimed_by="api:failure-worker",
    )

    fake_cloudmail.error = ""
    restored = client.get(
        "/api/v1/workbench/current",
        headers=_headers("failure-worker"),
        auth=("admin", "pass123"),
    )
    assert restored.status_code == 200
    assert restored.json()["mapping"]["id"] == mapping.id
    assert store.is_workbench_claim_baseline_ready(
        mapping.id,
        claimed_by="api:failure-worker",
    )


def test_external_api_supports_unique_icloud_aliases_and_additive_tags(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    root = store.create_mapping(
        "api-alias@icloud.com",
        query_email="openai@eve.ink",
        category="未使用",
    )
    gpt = store.create_tag("GPT", kind="service")
    fake_cloudmail = FakeCloudMailClient()
    client = TestClient(
        create_app(settings=_settings(tmp_path), store=store, cloudmail_client=fake_cloudmail)
    )
    source_id = store.get_category_id("未使用")
    headers = _headers("alias-worker")

    claimed_response = client.post(
        "/api/v1/workbench/claim-next",
        json={
            "category_id": source_id,
            "target_tag_id": gpt.id,
            "address_mode": "icloud_alias",
        },
        headers=headers,
        auth=("admin", "pass123"),
    )
    claimed = claimed_response.json()["mapping"]

    assert claimed_response.status_code == 200
    assert claimed["address_mode"] == "icloud_alias"
    assert claimed["registration_email"].startswith("api-alias+")
    first_alias = claimed["registration_email"]

    fake_cloudmail.messages = [
        CloudMailMessage(
            email_id=88,
            send_email="noreply@tm.openai.com",
            send_name="OpenAI",
            subject="Your GPT verification code 778899",
            to_email=root.query_email,
            to_name="",
            create_time="2099-01-01 00:00:00",
            type=0,
            content="<p>778899</p>",
            text="Your GPT verification code is 778899",
            is_del=0,
            recipient='[{"address":"api-alias@icloud.com"}]',
        )
    ]
    current = client.get(
        "/api/v1/workbench/current",
        headers=headers,
        auth=("admin", "pass123"),
    )
    assert current.json()["latest_code"] == "778899"
    assert current.json()["recipient_match"] == "root_fallback"

    completed = client.post(
        "/api/v1/workbench/complete",
        json={
            "mapping_id": claimed["id"],
            "category_id": source_id,
        },
        headers=headers,
        auth=("admin", "pass123"),
    )
    payload = completed.json()

    assert completed.status_code == 200
    assert payload["mapping"]["address_mode"] == "icloud_alias"
    assert payload["mapping"]["registration_email"] != first_alias
    assert {tag.name for tag in store.list_mapping_tags(root.id)} == {"未使用", "GPT"}
    assert store.count_verification_events(tag_id=gpt.id) == 1


def test_external_api_skip_turns_an_ownership_race_into_conflict(tmp_path, monkeypatch) -> None:
    store = KeyStore(tmp_path / "app.db")
    source_tag = store.create_tag("未使用", kind="business")
    target_tag = store.create_tag("GPT", kind="service")
    mapping = store.create_mapping(recipient_email="first@example.com", category=source_tag.name)
    client = TestClient(
        create_app(settings=_settings(tmp_path), store=store, cloudmail_client=FakeCloudMailClient())
    )
    category_id = source_tag.id
    client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": category_id, "target_tag_id": target_tag.id},
        headers=_headers("race-worker"),
        auth=("admin", "pass123"),
    )

    def stale_reset(_mapping_id: int, claimed_by: str | None = None):
        raise ValueError("mapping not claimed by this session")

    monkeypatch.setattr(store, "reset_mapping_status", stale_reset)
    response = client.post(
        "/api/v1/workbench/skip-current",
        json={"mapping_id": mapping.id, "category_id": category_id},
        headers=_headers("race-worker"),
        auth=("admin", "pass123"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workbench_error"


def test_external_api_invalid_skip_mode_does_not_release_current_mapping(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    source_tag = store.create_tag("未使用", kind="business")
    target_tag = store.create_tag("GPT", kind="service")
    mapping = store.create_mapping(recipient_email="invalid-mode@example.com", category=source_tag.name)
    client = TestClient(
        create_app(settings=_settings(tmp_path), store=store, cloudmail_client=FakeCloudMailClient())
    )
    category_id = source_tag.id
    headers = _headers("invalid-mode-worker")
    client.post(
        "/api/v1/workbench/claim-next",
        json={"category_id": category_id, "target_tag_id": target_tag.id},
        headers=headers,
        auth=("admin", "pass123"),
    )

    response = client.post(
        "/api/v1/workbench/skip-current",
        json={
            "mapping_id": mapping.id,
            "category_id": category_id,
            "address_mode": "unsupported",
        },
        headers=headers,
        auth=("admin", "pass123"),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "address_mode_invalid"
    assert store.get_by_id(mapping.id).status == "in_progress"
