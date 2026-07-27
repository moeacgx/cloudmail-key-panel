from __future__ import annotations

from fastapi.testclient import TestClient

from app.cloudmail import CloudMailError, CloudMailMessage
from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


class AdminMailboxClient:
    def __init__(self) -> None:
        self.messages: list[CloudMailMessage] = []
        self.error = ""

    def set_code(self, recipient: str, code: str) -> None:
        self.messages = [
            CloudMailMessage(
                email_id=len(self.messages) + 1,
                send_email="noreply@tm.openai.com",
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


def _client(tmp_path, store: KeyStore, cloudmail: AdminMailboxClient) -> TestClient:
    settings = AppSettings(
        app_secret_key="admin-registration-test",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_api_token="token",
    )
    client = TestClient(create_app(settings=settings, store=store, cloudmail_client=cloudmail))
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})
    return client


def test_admin_alias_mode_records_usage_on_root_and_can_repeat_same_service(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("可复用", kind="business")
    root = store.create_mapping("admin-alias@icloud.com", category="可复用")
    gpt = store.create_tag("GPT", kind="service")
    cloudmail = AdminMailboxClient()
    client = _client(tmp_path, store, cloudmail)

    claimed_response = client.post(
        "/api/workbench/claim-next",
        data={
            "category": "可复用",
            "target_tag_id": str(gpt.id),
            "address_mode": "icloud_alias",
        },
    )
    claimed = claimed_response.json()["mapping"]
    assert claimed_response.status_code == 200
    assert claimed["address_kind"] == "icloud_alias"
    assert claimed["parent_mapping_id"] == root.id
    assert claimed["recipient_email"].startswith("admin-alias+")

    # CloudMail 会丢掉 iCloud 的 +裂变部分，只保留主邮箱收件人。
    cloudmail.set_code(root.recipient_email, "551122")
    current_mailbox = client.get("/api/workbench/current/mailbox").json()
    assert current_mailbox["latest_code"] == "551122"
    assert current_mailbox["recipient_match"] == "root_fallback"
    completed_response = client.post(
        "/api/workbench/current/mark-used",
        data={
            "mapping_id": claimed["id"],
            "category": "可复用",
            "target_tag_id": str(gpt.id),
            "address_mode": "icloud_alias",
        },
    )
    payload = completed_response.json()

    assert completed_response.status_code == 200
    assert [tag.id for tag in store.list_mapping_tags(root.id) if tag.name == "GPT"] == [gpt.id]
    assert store.count_verification_events(tag_id=gpt.id) == 1
    assert store.get_tag(gpt.id).success_count == 1
    assert store.get_by_id(root.id).first_used_at
    assert payload["mapping"] is None

    next_response = client.post(
        "/api/workbench/claim-next",
        data={
            "category": "可复用",
            "target_tag_id": str(gpt.id),
            "address_mode": "icloud_alias",
        },
    )
    next_alias = next_response.json()["mapping"]
    assert next_response.status_code == 200
    assert next_alias["address_kind"] == "icloud_alias"
    assert next_alias["recipient_email"] != claimed["recipient_email"]

    second_alias_id = next_alias["id"]
    skipped = client.post(
        "/api/workbench/current/skip",
        data={
            "mapping_id": second_alias_id,
            "category": "可复用",
            "target_tag_id": str(gpt.id),
        },
    )
    assert skipped.status_code == 200
    assert store.get_by_id(second_alias_id).reuse_policy == "retired"
    assert store.get_by_id(root.id).reuse_policy == "reusable"

    fixed_same_service = client.post(
        "/api/workbench/claim-next",
        data={
            "category": "可复用",
            "target_tag_id": str(gpt.id),
            "address_mode": "primary",
        },
    )
    assert fixed_same_service.json()["mapping"] is None


def test_admin_prevent_pool_marks_previously_used_mailbox_as_retired(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("可复用", kind="business")
    mapping = store.create_mapping("used-private@icloud.com", category="可复用")
    old_tag = store.create_tag("Claude", kind="service")
    gpt = store.create_tag("GPT", kind="service")
    store.add_mapping_tag(mapping.id, old_tag.id, source="usage")
    # 模拟此前已经成功接过码。
    with store._connect() as connection:
        connection.execute(
            "UPDATE access_mappings SET first_used_at = '2026-01-01 00:00:00' WHERE id = ?",
            (mapping.id,),
        )
        connection.commit()

    cloudmail = AdminMailboxClient()
    client = _client(tmp_path, store, cloudmail)
    claimed = client.post(
        "/api/workbench/claim-next",
        data={
            "category": "可复用",
            "target_tag_id": str(gpt.id),
            "address_mode": "primary",
        },
    ).json()["mapping"]
    cloudmail.set_code(mapping.recipient_email, "991188")

    completed = client.post(
        "/api/workbench/current/mark-used",
        data={
            "mapping_id": claimed["id"],
            "category": "可复用",
            "target_tag_id": str(gpt.id),
            "prevent_shared_pool": "true",
        },
    )

    assert completed.status_code == 200
    assert store.get_by_id(mapping.id).reuse_policy == "retired"
    assert store.count_verification_events(tag_id=gpt.id) == 1


def test_admin_skip_releases_without_recording_an_arrived_code(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    mapping = store.create_mapping("skip-arrived@icloud.com", category="未使用")
    gpt = store.create_tag("GPT", kind="service")
    cloudmail = AdminMailboxClient()
    client = _client(tmp_path, store, cloudmail)
    claimed = client.post(
        "/api/workbench/claim-next",
        data={"category": "未使用", "target_tag_id": str(gpt.id)},
    ).json()["mapping"]
    cloudmail.set_code(mapping.recipient_email, "771122")

    skipped = client.post(
        "/api/workbench/current/skip",
        data={
            "mapping_id": claimed["id"],
            "target_tag_id": str(gpt.id),
            "prevent_shared_pool": "true",
        },
    )

    refreshed = store.get_by_id(mapping.id)
    assert skipped.status_code == 200
    assert "解除占用" in skipped.json()["message"]
    assert not refreshed.first_used_at
    assert refreshed.reuse_policy == "reusable"
    assert [tag.id for tag in store.list_mapping_tags(mapping.id) if tag.name == "GPT"] == []
    assert store.count_verification_events(tag_id=gpt.id) == 0


def test_admin_claim_next_refuses_to_replace_current_even_if_code_has_arrived(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    first = store.create_mapping("claim-next-first@icloud.com", category="未使用")
    second = store.create_mapping("claim-next-second@icloud.com", category="未使用")
    gpt = store.create_tag("GPT", kind="service")
    cloudmail = AdminMailboxClient()
    client = _client(tmp_path, store, cloudmail)
    claimed = client.post(
        "/api/workbench/claim-next",
        data={"category": "未使用", "target_tag_id": str(gpt.id)},
    ).json()["mapping"]
    assert claimed["id"] == first.id
    cloudmail.set_code(first.recipient_email, "881133")

    next_response = client.post(
        "/api/workbench/claim-next",
        data={
            "category": "未使用",
            "target_tag_id": str(gpt.id),
        },
    )

    assert next_response.status_code == 409
    assert "请先确认当前邮箱已接码" in next_response.json()["message"]
    assert store.get_by_id(first.id).status == "in_progress"
    assert store.get_by_id(second.id).status == "idle"
    assert not store.get_by_id(first.id).first_used_at
    assert [tag.id for tag in store.list_mapping_tags(first.id) if tag.name == "GPT"] == []
    assert store.count_verification_events(tag_id=gpt.id) == 0

    completed = client.post(
        "/api/workbench/current/mark-used",
        data={"mapping_id": str(first.id), "target_tag_id": str(gpt.id)},
    )
    assert completed.status_code == 200
    assert completed.json()["mapping"] is None
    assert store.get_by_id(first.id).first_used_at
    assert store.get_by_id(second.id).status == "idle"
    assert [tag.id for tag in store.list_mapping_tags(first.id) if tag.name == "GPT"] == [gpt.id]
    assert store.count_verification_events(tag_id=gpt.id) == 1


def test_admin_claim_snapshots_preexisting_mail_before_returning_mapping(
    tmp_path,
    monkeypatch,
) -> None:
    """后台领取响应不得把领取前已存在的同秒验证码当成本次验证码。"""

    monkeypatch.setattr(KeyStore, "_now", staticmethod(lambda: "2026-07-18 10:00:00"))
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    mapping = store.create_mapping("admin-snapshot@icloud.com", category="未使用")
    gpt = store.create_tag("GPT", kind="service")
    cloudmail = AdminMailboxClient()
    cloudmail.messages = [
        CloudMailMessage(
            email_id=201,
            send_email="noreply@tm.openai.com",
            send_name="OpenAI",
            subject="Your verification code is 201201",
            to_email=mapping.recipient_email,
            to_name="",
            create_time="2026-07-18 10:00:00",
            type=0,
            content="<p>201201</p>",
            text="Your verification code is 201201",
            is_del=0,
        )
    ]
    client = _client(tmp_path, store, cloudmail)

    claimed_response = client.post(
        "/api/workbench/claim-next",
        data={"category": "未使用", "target_tag_id": str(gpt.id)},
    )

    assert claimed_response.status_code == 200
    assert claimed_response.json()["mapping"]["id"] == mapping.id
    assert store.get_by_id(mapping.id).last_seen_email_id == 201
    old_poll = client.get("/api/workbench/current/mailbox")
    assert old_poll.status_code == 200
    assert old_poll.json()["latest_code"] is None

    cloudmail.messages.insert(
        0,
        CloudMailMessage(
            email_id=202,
            send_email="noreply@tm.openai.com",
            send_name="OpenAI",
            subject="Your verification code is 202202",
            to_email=mapping.recipient_email,
            to_name="",
            create_time="2026-07-18 10:00:00",
            type=0,
            content="<p>202202</p>",
            text="Your verification code is 202202",
            is_del=0,
        ),
    )
    current = client.get("/api/workbench/current/mailbox")

    assert current.status_code == 200
    assert current.json()["latest_code"] == "202202"


def test_admin_claim_retry_restores_uninitialized_snapshot_instead_of_skipping(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    mapping = store.create_mapping("admin-snapshot-retry@icloud.com", category="未使用")
    gpt = store.create_tag("GPT", kind="service")
    cloudmail = AdminMailboxClient()
    cloudmail.error = "upstream unavailable"
    client = _client(tmp_path, store, cloudmail)

    failed = client.post(
        "/api/workbench/claim-next",
        data={"category": "未使用", "target_tag_id": str(gpt.id)},
    )
    reserved = store.get_by_id(mapping.id)

    assert failed.status_code == 502
    assert failed.json()["error"]
    assert reserved is not None and reserved.status == "in_progress"
    assert not store.is_workbench_claim_baseline_ready(
        mapping.id,
        claimed_by=reserved.claimed_by,
    )

    cloudmail.error = ""
    restored = client.post(
        "/api/workbench/claim-next",
        data={"category": "未使用", "target_tag_id": str(gpt.id)},
    )

    assert restored.status_code == 200
    assert restored.json()["mapping"]["id"] == mapping.id
    assert restored.json()["message"] == "已恢复此前预留的邮箱"
    assert store.is_workbench_claim_baseline_ready(
        mapping.id,
        claimed_by=reserved.claimed_by,
    )


def test_admin_first_successful_mailbox_query_only_establishes_delivery_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    """补快照与首次查码必须分成两次请求，邮箱交付前不能提前返回验证码。"""

    monkeypatch.setattr(KeyStore, "_now", staticmethod(lambda: "2026-07-18 10:00:00"))
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    mapping = store.create_mapping("admin-delivery-boundary@icloud.com", category="未使用")
    gpt = store.create_tag("GPT", kind="service")
    old_message = CloudMailMessage(
        email_id=401,
        send_email="noreply@tm.openai.com",
        send_name="OpenAI",
        subject="Your verification code is 401401",
        to_email=mapping.recipient_email,
        to_name="",
        create_time="2026-07-18 10:00:00",
        type=0,
        content="<p>401401</p>",
        text="Your verification code is 401401",
        is_del=0,
    )
    after_snapshot_message = CloudMailMessage(
        email_id=402,
        send_email="noreply@tm.openai.com",
        send_name="OpenAI",
        subject="Your verification code is 402402",
        to_email=mapping.recipient_email,
        to_name="",
        create_time="2026-07-18 10:00:01",
        type=0,
        content="<p>402402</p>",
        text="Your verification code is 402402",
        is_del=0,
    )

    class DeliveryBoundaryClient(AdminMailboxClient):
        def __init__(self) -> None:
            super().__init__()
            self.successful_calls = 0

        def fetch_recent_emails(self, recipient_email: str, limit: int = 10):
            if self.error:
                raise CloudMailError(self.error)
            self.successful_calls += 1
            if self.successful_calls == 1:
                return [old_message]
            return [after_snapshot_message, old_message]

    cloudmail = DeliveryBoundaryClient()
    cloudmail.error = "upstream unavailable"
    client = _client(tmp_path, store, cloudmail)

    failed = client.post(
        "/api/workbench/claim-next",
        data={"category": "未使用", "target_tag_id": str(gpt.id)},
    )
    assert failed.status_code == 502

    cloudmail.error = ""
    delivered = client.get("/api/workbench/current/mailbox")

    assert delivered.status_code == 200
    assert delivered.json()["latest_code"] is None
    assert cloudmail.successful_calls == 1
    assert store.get_by_id(mapping.id).last_seen_email_id == 401

    polled = client.get("/api/workbench/current/mailbox")
    assert polled.status_code == 200
    assert polled.json()["latest_code"] == "402402"


def test_admin_can_delete_unused_tag_but_must_archive_used_tag(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    unused = store.create_tag("可删除标签", kind="business")
    used = store.create_tag("已有邮箱标签", kind="business")
    store.create_mapping("tag-history@example.com", category=used.name)
    client = _client(tmp_path, store, AdminMailboxClient())

    deleted = client.post(f"/admin/tags/{unused.id}/delete")

    assert deleted.status_code == 200
    assert "标签已彻底删除" in deleted.text
    assert store.get_tag(unused.id) is None

    rejected = client.post(f"/admin/tags/{used.id}/delete")

    assert rejected.status_code == 200
    assert "请使用归档保留历史" in rejected.text
    assert store.get_tag(used.id) is not None
    assert f'action="/admin/tags/{used.id}/delete"' in rejected.text
    assert 'class="rounded-box border border-base-300 px-3 py-2"' in rejected.text
