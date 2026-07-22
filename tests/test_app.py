from fastapi.testclient import TestClient

from app.cloudmail import CloudMailMessage
from app.main import _build_preview, create_app
from app.settings import AppSettings
from app.store import KeyStore


class FakeCloudMailClient:
    def __init__(self, messages: list[CloudMailMessage] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.messages = [
            CloudMailMessage(
                email_id=1,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="Your ChatGPT code is 330119",
                to_email="",
                to_name="buyer",
                create_time="2099-01-01 00:00:00",
                type=0,
                content="<div>ChatGPT Log-in Code</div><div>330119</div>",
                text="ChatGPT Log-in Code\n330119",
                is_del=0,
            )
        ] if messages is None else messages

    def fetch_recent_emails(self, recipient_email: str, limit: int = 10) -> list[CloudMailMessage]:
        self.calls.append((recipient_email, limit))
        return [
            CloudMailMessage(
                email_id=message.email_id,
                send_email=message.send_email,
                send_name=message.send_name,
                subject=message.subject,
                to_email=message.to_email or recipient_email,
                to_name=message.to_name,
                create_time=message.create_time,
                type=message.type,
                content=message.content,
                text=message.text,
                is_del=message.is_del,
                recipient=message.recipient,
            )
            for message in self.messages[:limit]
        ]


class FakeCloudMailFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.configs: list[object] = []

    def __call__(self, config):
        self.configs.append(config)
        factory = self

        class _Client:
            def fetch_recent_emails(self, recipient_email: str, limit: int = 10) -> list[CloudMailMessage]:
                factory.calls.append((recipient_email, limit))
                return [
                    CloudMailMessage(
                        email_id=1,
                        send_email="noreply@tm.openai.com",
                        send_name="OpenAI",
                        subject="Your ChatGPT code is 330119",
                        to_email=recipient_email,
                        to_name="buyer",
                        create_time="2099-01-01 00:00:00",
                        type=0,
                        content="<div>ChatGPT Log-in Code</div><div>330119</div>",
                        text="ChatGPT Log-in Code\n330119",
                        is_del=0,
                    )
                ]

        return _Client()


def test_admin_can_create_mapping_and_public_lookup_shows_recent_codes(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient()
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    login_response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200
    assert "创建查看 Key" in login_response.text

    create_response = client.post(
        "/admin/keys",
        data={
            "recipient_email": "cranes_solute.1o@icloud.com",
            "query_email": "cranes_solute.1o@icloud.com",
            "access_key": "demo-key-001",
            "label": "order-1",
            "category": "OpenAI OTP",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert "demo-key-001" in create_response.text
    assert "cranes_solute.1o@icloud.com" in create_response.text
    assert "OpenAI OTP" in create_response.text
    assert "复制链接" in create_response.text
    assert "导出选中链接" in create_response.text

    lookup_response = client.post(
        "/lookup",
        data={"access_key": "demo-key-001"},
        follow_redirects=True,
    )

    assert lookup_response.status_code == 200
    assert "330119" in lookup_response.text
    assert "noreply@tm.openai.com" in lookup_response.text
    assert "cranes_solute.1o@icloud.com" in lookup_response.text
    assert "CloudMail 查询邮箱" not in lookup_response.text
    assert "发件人" in lookup_response.text
    assert "收件人" in lookup_response.text
    assert fake_cloudmail.calls == [("cranes_solute.1o@icloud.com", 5)]



def test_admin_can_save_cloudmail_settings_and_lookup_uses_saved_token(tmp_path) -> None:
    fake_factory = FakeCloudMailFactory()
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://env.example.com",
        cloudmail_api_token="env-token",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, cloudmail_client_factory=fake_factory)
    client = TestClient(app)

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )

    save_response = client.post(
        "/admin/cloudmail",
        data={
            "base_url": "https://mail.boxmoe.eu.org/",
            "api_token": "fixed-token-123",
            "internal_admin_email": "admin@example.com",
            "internal_admin_password": "secret",
            "default_query_email": "openai@eve.ink",
            "recent_email_limit": "3",
            "display_timezone": "Asia/Shanghai",
        },
        follow_redirects=True,
    )

    assert save_response.status_code == 200
    assert "CloudMail 配置已保存" in save_response.text
    assert "https://mail.boxmoe.eu.org/" in save_response.text
    assert 'name="query_email" value="openai@eve.ink"' in save_response.text
    assert 'name="internal_admin_email" value="admin@example.com"' in save_response.text
    assert 'name="recent_email_limit" min="1" step="1" value="3"' in save_response.text
    assert 'name="display_timezone" value="Asia/Shanghai"' in save_response.text
    assert 'id="cloudmail-config-dialog"' in save_response.text
    assert 'data-cloudmail-config-trigger' in save_response.text
    assert "编辑 CloudMail 配置" in save_response.text
    assert "系统时区" in save_response.text

    client.post(
        "/admin/keys",
        data={
            "recipient_email": "buyer@example.com",
            "query_email": "",
            "access_key": "buyer-key-1",
            "label": "buyer-order",
        },
        follow_redirects=True,
    )
    lookup_response = client.post(
        "/lookup",
        data={"access_key": "buyer-key-1"},
        follow_redirects=True,
    )

    assert lookup_response.status_code == 200
    assert fake_factory.calls == [("openai@eve.ink", 3)]
    assert fake_factory.configs[-1].base_url == "https://mail.boxmoe.eu.org/"
    assert fake_factory.configs[-1].api_token == "fixed-token-123"
    assert fake_factory.configs[-1].internal_admin_email == "admin@example.com"
    assert fake_factory.configs[-1].internal_admin_password == "secret"


def test_admin_can_save_ai_extraction_config_without_rendering_api_key(tmp_path) -> None:
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    store = KeyStore(tmp_path / "app.db")
    client = TestClient(create_app(settings=settings, store=store))
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})

    response = client.post(
        "/admin/verification-extraction",
        data={
            "mode": "fallback",
            "custom_patterns": "token=([0-9]{4,8})\norder=([A-Z0-9-]+)",
            "base_url": "https://ai.example.com/v1",
            "api_key": "do-not-render-this-key",
            "model": "extract-model",
            "timeout_seconds": "7",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "验证码提取配置已保存" in response.text
    assert "extract-model" in response.text
    assert 'option value="fallback" selected' in response.text
    assert "data-verification-test" in response.text
    assert 'data-rule-preset="digits6"' in response.text
    assert "自定义组合规则" in response.text
    assert "点击变量，按验证码出现顺序拼接" in response.text
    assert "高级：直接编辑正则" in response.text
    assert "AI 兜底接口" in response.text
    assert "max-w-4xl" in response.text
    assert "token=([0-9]{4,8})" in response.text
    assert "do-not-render-this-key" not in response.text
    saved = store.get_verification_extraction_settings()
    assert saved.api_key == "do-not-render-this-key"
    assert saved.timeout_seconds == 7
    assert saved.mode == "fallback"
    assert saved.custom_patterns == (
        "token=([0-9]{4,8})",
        "order=([A-Z0-9-]+)",
    )

    client.post(
        "/admin/verification-extraction",
        data={
            "mode": "fallback",
            "custom_patterns": "token=([0-9]{4,8})\norder=([A-Z0-9-]+)",
            "base_url": "https://ai.example.com/v1",
            "api_key": "",
            "model": "extract-model-v2",
            "timeout_seconds": "9",
        },
    )
    assert store.get_verification_extraction_settings().api_key == "do-not-render-this-key"


def test_public_pages_do_not_show_top_nav_buttons(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient()
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )
    client.post(
        "/admin/keys",
        data={
            "recipient_email": "buyer@example.com",
            "query_email": "openai@eve.ink",
            "access_key": "buyer-key-1",
            "label": "buyer-order",
        },
        follow_redirects=True,
    )

    home_response = client.get("/")
    mailbox_response = client.get("/mailbox/buyer-key-1")

    for page in (home_response.text, mailbox_response.text):
        assert "前台查询" not in page
        assert "后台管理" not in page

    assert "items-center text-center" in home_response.text


def test_admin_can_edit_and_delete_key(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient()
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )
    create_response = client.post(
        "/admin/keys",
        data={
            "recipient_email": "buyer@example.com",
            "query_email": "openai@eve.ink",
            "access_key": "buyer-key-1",
            "label": "buyer-order",
        },
        follow_redirects=True,
    )
    assert "buyer-key-1" in create_response.text
    assert ">编辑</button>" in create_response.text
    assert 'id="mapping-edit-dialog"' in create_response.text
    assert 'name="recipient_email" value="buyer@example.com"' not in create_response.text
    assert 'action="/admin/keys/1/update"' not in create_response.text

    edit_response = client.post(
        "/admin/keys/1/update",
        data={
            "recipient_email": "buyer2@example.com",
            "query_email": "mail@eve.ink",
            "access_key": "buyer-key-2",
            "label": "buyer-order-2",
        },
        follow_redirects=True,
    )
    assert edit_response.status_code == 200
    assert "buyer-key-2" in edit_response.text
    assert "buyer2@example.com" in edit_response.text
    assert "mail@eve.ink" in edit_response.text

    lookup_response = client.post(
        "/lookup",
        data={"access_key": "buyer-key-2"},
        follow_redirects=True,
    )
    assert lookup_response.status_code == 200
    assert fake_cloudmail.calls[-1] == ("mail@eve.ink", 5)

    delete_response = client.post(
        "/admin/keys/1/delete",
        follow_redirects=True,
    )
    assert delete_response.status_code == 200
    assert "buyer-key-2" not in delete_response.text

    missing_lookup = client.get("/mailbox/buyer-key-2")
    assert missing_lookup.status_code == 404


def test_admin_dashboard_supports_key_search_pagination_and_category_filter(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    for index in range(1, 13):
        label = "special-order" if index == 7 else f"order-{index}"
        category = "OpenAI" if index % 2 else "Apple"
        store.create_mapping(
            recipient_email=f"buyer{index:02d}@example.com",
            query_email="openai@eve.ink",
            access_key=f"buyer-key-{index:02d}",
            label=label,
            category=category,
        )

    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, store=store, cloudmail_client=FakeCloudMailClient())
    client = TestClient(app)

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )

    search_response = client.get("/admin?q=special")
    filter_response = client.get("/admin?category=OpenAI")
    page_two_response = client.get("/admin?page=2")

    assert search_response.status_code == 200
    assert 'name="q" value="special"' in search_response.text
    assert "buyer-key-07" in search_response.text
    assert "buyer-key-12" not in search_response.text

    assert filter_response.status_code == 200
    assert 'name="category"' in filter_response.text
    assert 'option value="OpenAI" selected' in filter_response.text
    assert "buyer-key-11" in filter_response.text
    assert "buyer-key-12" not in filter_response.text
    assert "复制链接" in filter_response.text
    assert "导出选中链接" in filter_response.text
    assert "btn btn-outline btn-sm" in filter_response.text

    assert page_two_response.status_code == 200
    assert "buyer-key-02" in page_two_response.text
    assert "buyer-key-01" in page_two_response.text
    assert "buyer-key-12" not in page_two_response.text
    assert "第 2 / 2 页" in page_two_response.text


def test_admin_dashboard_distinguishes_success_platform_tag_and_never_used(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    business_tagged = store.create_mapping("business@example.com")
    platform_tagged = store.create_mapping("platform@example.com")
    successful = store.create_mapping("successful@example.com")
    business_tag = store.create_tag("库存 A", kind="business")
    platform_tag = store.create_tag("OpenAI", kind="service")
    store.add_mapping_tag(business_tagged.id, business_tag.id, source="manual")
    store.add_mapping_tag(platform_tagged.id, platform_tag.id, source="manual")
    with store._connect() as connection:
        connection.execute(
            "UPDATE access_mappings SET first_used_at = ? WHERE id = ?",
            ("2026-07-21 00:00:00", successful.id),
        )
        connection.commit()

    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    client = TestClient(
        create_app(settings=settings, store=store, cloudmail_client=FakeCloudMailClient())
    )
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})

    response = client.get("/admin")

    assert response.status_code == 200
    assert response.text.count("完全未使用</span>") == 1
    assert response.text.count("已有平台使用标签</span>") == 1
    assert response.text.count("已成功接码</span>") == 1


def test_admin_mapping_forms_use_one_compact_multi_tag_selector(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    unused_tag = store.create_tag("未使用", kind="business")
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, store=store, cloudmail_client=FakeCloudMailClient())
    client = TestClient(app)
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})

    dashboard_response = client.get("/admin")
    create_response = client.post(
        "/admin/keys",
        data={
            "recipient_email": "custom@example.com",
            "query_email": "custom@example.com",
            "tag_ids": str(unused_tag.id),
        },
        follow_redirects=True,
    )

    assert 'data-category-select' not in dashboard_response.text
    assert "其他标签（可多选）" not in dashboard_response.text
    assert "邮箱标签（可多选）" in dashboard_response.text
    assert "全部标签" in dashboard_response.text
    assert 'class="checkbox checkbox-xs"' in dashboard_response.text
    assert create_response.status_code == 200
    created = next(mapping for mapping in store.list_mappings() if mapping.recipient_email == "custom@example.com")
    assert created.category == "未使用"
    assert [tag.id for tag in store.list_mapping_tags(created.id)] == [unused_tag.id]


def test_admin_create_key_panel_is_collapsed_by_default_and_opens_after_create_error(tmp_path) -> None:
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    client = TestClient(create_app(settings=settings, cloudmail_client=FakeCloudMailClient()))
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )

    dashboard_response = client.get("/admin")
    assert dashboard_response.status_code == 200
    assert "data-create-key-panel>" in dashboard_response.text
    assert "data-create-key-panel open>" not in dashboard_response.text

    error_response = client.post(
        "/admin/keys",
        data={
            "recipient_email": "first@example.com\nsecond@example.com",
            "query_email": "first@example.com",
            "access_key": "cannot-share-one-key",
        },
    )

    assert error_response.status_code == 400
    assert "批量导入多个邮箱时不能自定义单个 Key" in error_response.text
    assert "data-create-key-panel open>" in error_response.text


def test_admin_can_reset_occupied_mapping_without_dashboard_navigation(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping(
        recipient_email="occupied@example.com",
        query_email="occupied@example.com",
        access_key="occupied-key",
        category="未使用",
    )
    store.claim_next_available_mapping(category_filter="未使用", claimed_by="other-session")
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, store=store, cloudmail_client=FakeCloudMailClient())
    client = TestClient(app)
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})

    dashboard_response = client.get("/admin")
    reset_response = client.post(f"/api/admin/keys/{mapping.id}/reset-status")

    assert 'data-reset-status data-id="' in dashboard_response.text
    assert reset_response.status_code == 200
    assert reset_response.json()["mapping"]["status"] == "idle"
    assert store.get_by_id(mapping.id).status == "idle"



def test_admin_can_batch_delete_keys_and_export_links(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    first = store.create_mapping(
        recipient_email="buyer1@example.com",
        query_email="openai@eve.ink",
        access_key="buyer-key-1",
        label="first",
        category="OpenAI",
    )
    second = store.create_mapping(
        recipient_email="buyer2@example.com",
        query_email="openai@eve.ink",
        access_key="buyer-key-2",
        label="second",
        category="Apple",
    )
    third = store.create_mapping(
        recipient_email="buyer3@example.com",
        query_email="openai@eve.ink",
        access_key="buyer-key-3",
        label="third",
        category="OpenAI",
    )

    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, store=store, cloudmail_client=FakeCloudMailClient())
    client = TestClient(app)

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )

    dashboard_response = client.get("/admin")
    assert dashboard_response.status_code == 200
    assert "导出选中链接" in dashboard_response.text
    assert 'id="export-links-dialog"' in dashboard_response.text
    assert 'data-copy-link-url="http://testserver/mailbox/buyer-key-1"' in dashboard_response.text

    response = client.post(
        "/admin/keys/batch-delete",
        data={"mapping_ids": [str(first.id), str(third.id)]},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "已批量删除 2 个 Key" in response.text
    assert "buyer-key-1" not in response.text
    assert "buyer-key-3" not in response.text
    assert "buyer-key-2" in response.text
    assert store.get_by_key("buyer-key-1") is None
    assert store.get_by_key(third.access_key) is None
    assert store.get_by_key(second.access_key) is not None



def test_admin_can_batch_create_keys_from_multiple_recipient_lines(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, store=store, cloudmail_client=FakeCloudMailClient())
    client = TestClient(app)

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )

    response = client.post(
        "/admin/keys",
        data={
            "recipient_email": "buyer1@example.com\nbuyer2@example.com\n\nbuyer3@example.com",
            "query_email": "openai@eve.ink",
            "access_key": "",
            "label": "bundle-order",
            "category": "ChatGPT",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "已批量创建 3 个 Key" in response.text
    assert 'name="recipient_email"' in response.text
    assert "textarea-bordered" in response.text
    assert "邮箱标签（可多选）" in response.text
    assert store.count_mappings() == 3
    assert all(mapping.category == "ChatGPT" for mapping in store.list_mappings())
    assert {mapping.recipient_email for mapping in store.list_mappings()} == {
        "buyer1@example.com",
        "buyer2@example.com",
        "buyer3@example.com",
    }
    assert all(mapping.access_key for mapping in store.list_mappings())


def test_admin_workbench_claims_mailbox_reads_codes_and_moves_to_next(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient(
        messages=[
            CloudMailMessage(
                email_id=1,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="Your code is 330119",
                to_email="first@example.com",
                to_name="buyer",
                create_time="2099-01-01 00:00:00",
                type=0,
                content="<div>Your code is 330119</div>",
                text="Your code is 330119",
                is_del=0,
            )
        ]
    )
    verification_message = fake_cloudmail.messages[0]
    fake_cloudmail.messages = []
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("ChatGPT", kind="business")
    platform_tag = store.create_tag("GPT", kind="service")
    first = store.create_mapping(
        recipient_email="first@example.com",
        query_email="first@example.com",
        access_key="first-key",
        category="ChatGPT",
    )
    second = store.create_mapping(
        recipient_email="second@example.com",
        query_email="second@example.com",
        access_key="second-key",
        category="ChatGPT",
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, store=store, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    unauthorized_response = client.get("/api/workbench/current")
    assert unauthorized_response.status_code == 401

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )

    dashboard_response = client.get("/admin")
    workbench_response = client.get("/admin/workbench")
    current_response = client.get("/api/workbench/current?category=ChatGPT")

    assert "打开注册工作台" in dashboard_response.text
    assert workbench_response.status_code == 200
    assert "注册工作台" in workbench_response.text
    assert "领取下一个" in workbench_response.text
    assert 'class="grid min-w-0 gap-1" data-current-meta-wrap' in workbench_response.text
    assert 'class="block min-w-0 max-w-full truncate" data-current-meta' in workbench_response.text
    assert "const truncateMetaPart = (value, maxLength = 16)" in workbench_response.text
    assert "currentMeta.title = fullMeta" in workbench_response.text
    assert 'rounded-field border border-error/40 bg-white' in workbench_response.text
    assert 'text-error shadow-sm transition-colors hover:border-error hover:bg-white hover:text-error' in workbench_response.text
    assert 'class="btn btn-sm' not in workbench_response.text.split('data-current-code')[0].rsplit('<button', 1)[-1]
    assert "const renderLatestCode = (payload)" in workbench_response.text
    assert "setLatestCode(payload.latest_code" in workbench_response.text
    assert "实时验证码" not in workbench_response.text
    assert "data-mailbox-output" not in workbench_response.text
    assert 'class="pointer-events-none fixed inset-0 z-50 flex items-center justify-center' in workbench_response.text
    assert "const showCopyToast = (message)" in workbench_response.text
    assert "showCopyToast('验证码已复制')" in workbench_response.text
    assert "setMessage('验证码已复制'" not in workbench_response.text
    assert '<option value="" selected disabled>请选择平台标签</option>' in workbench_response.text
    assert 'data-workbench-target-tag' in workbench_response.text
    assert "data.set('target_tag_id', targetTagInput?.value || '');" in workbench_response.text
    assert "setMessage('请先选择本次接码平台。', 'error');" in workbench_response.text
    assert "complete_category" not in workbench_response.text
    assert "cloudmail-workbench-private-v1" in workbench_response.text
    assert "window.localStorage.setItem(PRIVATE_PREFERENCE_KEY" in workbench_response.text
    assert "mapping.address_kind === 'icloud_alias'" in workbench_response.text
    assert workbench_response.text.count("[claimNextButton, markUsedButton, skipCurrentButton].forEach") == 4
    assert current_response.json()["mapping"] is None

    claim_response = client.post(
        "/api/workbench/claim-next",
        data={"category": "ChatGPT", "target_tag_id": str(platform_tag.id)},
    )
    claimed = claim_response.json()["mapping"]

    assert claim_response.status_code == 200
    assert claimed["id"] == first.id
    assert claimed["recipient_email"] == "first@example.com"
    assert claimed["status"] == "in_progress"
    assert claimed["status_label"] == "注册中"
    assert claimed["mailbox_url"].endswith("/mailbox/first-key")
    assert store.get_by_id(first.id).status == "in_progress"

    fake_cloudmail.messages = [verification_message]
    mailbox_response = client.get("/api/workbench/current/mailbox?category=ChatGPT")
    mailbox_payload = mailbox_response.json()

    assert mailbox_response.status_code == 200
    assert mailbox_payload["mapping"]["id"] == first.id
    assert mailbox_payload["latest_code"] == "330119"
    assert "emails" not in mailbox_payload
    assert "display_timezone" not in mailbox_payload
    assert fake_cloudmail.calls[-1] == ("first@example.com", 5)

    invalid_platform_response = client.post(
        "/api/workbench/current/mark-used",
        data={
            "mapping_id": str(first.id),
            "category": "ChatGPT",
            "target_tag_id": "999999",
        },
    )

    assert invalid_platform_response.status_code == 400
    assert invalid_platform_response.json()["error"] == "请选择有效的平台标签"
    assert store.get_by_id(first.id).status == "in_progress"
    assert store.get_by_id(first.id).category == "ChatGPT"

    mark_used_response = client.post(
        "/api/workbench/current/mark-used",
        data={
            "mapping_id": str(first.id),
            "category": "ChatGPT",
            "target_tag_id": str(platform_tag.id),
        },
    )
    mark_used_payload = mark_used_response.json()

    assert mark_used_response.status_code == 200
    assert mark_used_payload["completed"]["status"] == "idle"
    assert mark_used_payload["completed"]["category"] == "ChatGPT"
    assert mark_used_payload["mapping"]["id"] == second.id
    assert mark_used_payload["mapping"]["status"] == "in_progress"
    assert store.get_by_id(first.id).status == "idle"
    assert store.get_by_id(first.id).category == "ChatGPT"
    assert store.get_by_id(second.id).status == "in_progress"
    assert {tag.name for tag in store.list_mapping_tags(first.id)} == {"ChatGPT", "GPT"}
    assert store.count_verification_events(tag_id=platform_tag.id) == 1

    # 第二条当前没有验证码，跳过才应只释放而不记录使用。
    fake_cloudmail.messages = []
    skip_response = client.post(
        "/api/workbench/current/skip",
        data={
            "mapping_id": str(second.id),
            "category": "ChatGPT",
            "target_tag_id": str(platform_tag.id),
        },
    )
    skip_payload = skip_response.json()

    assert skip_response.status_code == 200
    assert skip_payload["completed"]["status"] == "idle"
    assert skip_payload["completed"]["category"] == "ChatGPT"
    assert skip_payload["mapping"] is None
    assert store.get_by_id(second.id).status == "idle"
    assert store.get_by_id(second.id).category == "ChatGPT"

    reclaim_response = client.post(
        "/api/workbench/claim-next",
        data={"category": "ChatGPT", "target_tag_id": str(platform_tag.id)},
    )
    reclaimed = reclaim_response.json()["mapping"]

    assert reclaim_response.status_code == 200
    assert reclaimed["id"] == second.id
    assert reclaimed["status"] == "in_progress"

    dashboard_reset_response = client.post(
        f"/admin/keys/{second.id}/reset-status",
        data={"q": "", "category": "ChatGPT", "page": "1"},
        follow_redirects=True,
    )

    assert dashboard_reset_response.status_code == 200
    assert "工作台占用已取消" in dashboard_reset_response.text
    assert store.get_by_id(second.id).status == "idle"
    assert store.get_by_id(second.id).category == "ChatGPT"

    reclaim_again_response = client.post(
        "/api/workbench/claim-next",
        data={"category": "ChatGPT", "target_tag_id": str(platform_tag.id)},
    )
    assert reclaim_again_response.json()["mapping"]["id"] == second.id

    fake_cloudmail.messages = [
        CloudMailMessage(
            email_id=2,
            send_email="noreply@tm.openai.com",
            send_name="OpenAI",
            subject="Your code is 440220",
            to_email=second.recipient_email,
            to_name="buyer",
            create_time="2099-01-01 00:00:00",
            type=0,
            content="<div>Your code is 440220</div>",
            text="Your code is 440220",
            is_del=0,
        )
    ]
    complete_second_response = client.post(
        "/api/workbench/current/mark-used",
        data={
            "mapping_id": str(second.id),
            "category": "ChatGPT",
            "target_tag_id": str(platform_tag.id),
        },
    )
    complete_second_payload = complete_second_response.json()

    assert complete_second_response.status_code == 200
    assert complete_second_payload["completed"]["status"] == "idle"
    assert complete_second_payload["completed"]["category"] == "ChatGPT"
    assert complete_second_payload["mapping"] is None
    assert store.get_by_id(second.id).status == "idle"
    assert store.get_by_id(second.id).category == "ChatGPT"
    assert store.count_verification_events(tag_id=platform_tag.id) == 2


def test_admin_workbench_all_categories_advances_after_completed_mapping(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("gpt废号", kind="business")
    platform_tag = store.create_tag("GPT", kind="service")
    first = store.create_mapping(
        recipient_email="first@example.com",
        query_email="first@example.com",
        access_key="first-key",
        category="gpt废号",
    )
    second = store.create_mapping(
        recipient_email="second@example.com",
        query_email="second@example.com",
        access_key="second-key",
        category="gpt废号",
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    app = create_app(
        settings=settings,
        store=store,
        cloudmail_client=FakeCloudMailClient(
            messages=[
                CloudMailMessage(
                    email_id=11,
                    send_email="noreply@tm.openai.com",
                    send_name="OpenAI",
                    subject="Your code is 111111",
                    to_email="first@example.com",
                    to_name="",
                    create_time="2099-01-01 00:01:00",
                    type=0,
                    content="<p>111111</p>",
                    text="111111",
                    is_del=0,
                ),
                CloudMailMessage(
                    email_id=12,
                    send_email="noreply@tm.openai.com",
                    send_name="OpenAI",
                    subject="Your code is 222222",
                    to_email="second@example.com",
                    to_name="",
                    create_time="2099-01-01 00:02:00",
                    type=0,
                    content="<p>222222</p>",
                    text="222222",
                    is_del=0,
                ),
            ]
        ),
    )
    fake_cloudmail = app.state.fixed_cloudmail_client
    queued_messages = list(fake_cloudmail.messages)
    fake_cloudmail.messages = []
    client = TestClient(app)
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )

    workbench_response = client.get("/admin/workbench")
    claim_response = client.post(
        "/api/workbench/claim-next",
        data={"category": "", "target_tag_id": str(platform_tag.id)},
    )
    assert workbench_response.status_code == 200
    assert "正在记录成功接码并领取下一个" in workbench_response.text
    assert 'value="gpt废号"' in workbench_response.text
    assert 'value="GPT废号"' not in workbench_response.text
    assert claim_response.json()["mapping"]["id"] == first.id

    fake_cloudmail.messages = [queued_messages[0]]
    first_complete_response = client.post(
        "/api/workbench/current/mark-used",
        data={
            "mapping_id": str(first.id),
            "category": "",
            "target_tag_id": str(platform_tag.id),
        },
    )
    first_complete_payload = first_complete_response.json()

    assert first_complete_response.status_code == 200
    assert first_complete_payload["completed"]["id"] == first.id
    assert first_complete_payload["completed"]["category"] == "gpt废号"
    assert first_complete_payload["mapping"]["id"] == second.id

    fake_cloudmail.messages = queued_messages
    second_complete_response = client.post(
        "/api/workbench/current/mark-used",
        data={
            "mapping_id": str(second.id),
            "category": "",
            "target_tag_id": str(platform_tag.id),
        },
    )
    second_complete_payload = second_complete_response.json()

    assert second_complete_response.status_code == 200
    assert second_complete_payload["completed"]["id"] == second.id
    assert second_complete_payload["mapping"] is None
    assert "暂无下一个可领取邮箱" in second_complete_payload["message"]
    assert store.count_verification_events(tag_id=platform_tag.id) == 2


def test_admin_workbench_claim_next_button_advances_current_mapping(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    platform_tag = store.create_tag("GPT", kind="service")
    first = store.create_mapping(recipient_email="first@example.com", access_key="first-key")
    second = store.create_mapping(recipient_email="second@example.com", access_key="second-key")
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    client = TestClient(
        create_app(settings=settings, store=store, cloudmail_client=FakeCloudMailClient(messages=[]))
    )
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )

    claim_data = {"category": "", "target_tag_id": str(platform_tag.id)}
    first_claim = client.post("/api/workbench/claim-next", data=claim_data).json()
    second_claim = client.post("/api/workbench/claim-next", data=claim_data).json()
    no_more = client.post("/api/workbench/claim-next", data=claim_data).json()

    assert first_claim["mapping"]["id"] == first.id
    assert second_claim["mapping"]["id"] == second.id
    assert second_claim["message"] == "已跳过当前邮箱并领取下一个"
    assert no_more["mapping"] is None
    assert no_more["message"] == "当前邮箱已释放，暂无下一个可领取邮箱"
    assert store.get_by_id(first.id).status == "idle"
    assert store.get_by_id(second.id).status == "idle"


def test_admin_workbench_switching_category_can_claim_an_earlier_mapping(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("Category A", kind="business")
    store.create_tag("Category B", kind="business")
    platform_tag = store.create_tag("GPT", kind="service")
    earlier_other_category = store.create_mapping(
        recipient_email="earlier@example.com",
        category="Category B",
    )
    current = store.create_mapping(
        recipient_email="current@example.com",
        category="Category A",
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    client = TestClient(
        create_app(settings=settings, store=store, cloudmail_client=FakeCloudMailClient(messages=[]))
    )
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )

    first_claim = client.post(
        "/api/workbench/claim-next",
        data={"category": "Category A", "target_tag_id": str(platform_tag.id)},
    )
    switched_claim = client.post(
        "/api/workbench/claim-next",
        data={"category": "Category B", "target_tag_id": str(platform_tag.id)},
    )

    assert first_claim.json()["mapping"]["id"] == current.id
    assert switched_claim.status_code == 200
    assert switched_claim.json()["mapping"]["id"] == earlier_other_category.id
    assert store.get_by_id(current.id).status == "idle"


def test_admin_workbench_current_mapping_is_isolated_by_browser_session(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient()
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("ChatGPT", kind="business")
    platform_tag = store.create_tag("GPT", kind="service")
    first = store.create_mapping(
        recipient_email="first@example.com",
        query_email="first@example.com",
        access_key="first-key",
        category="ChatGPT",
    )
    second = store.create_mapping(
        recipient_email="second@example.com",
        query_email="second@example.com",
        access_key="second-key",
        category="ChatGPT",
    )
    third = store.create_mapping(
        recipient_email="third@example.com",
        query_email="third@example.com",
        access_key="third-key",
        category="ChatGPT",
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, store=store, cloudmail_client=fake_cloudmail)
    client_a = TestClient(app)
    client_b = TestClient(app)

    for client in (client_a, client_b):
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "pass123"},
            follow_redirects=True,
        )
        assert response.status_code == 200

    first_claim_response = client_a.post(
        "/api/workbench/claim-next",
        data={"category": "ChatGPT", "target_tag_id": str(platform_tag.id)},
    )
    first_claim = first_claim_response.json()["mapping"]

    assert first_claim_response.status_code == 200
    assert first_claim["id"] == first.id
    assert store.get_by_id(first.id).status == "in_progress"
    assert store.get_by_id(first.id).claimed_by

    b_current_response = client_b.get("/api/workbench/current?category=ChatGPT")
    assert b_current_response.status_code == 200
    assert b_current_response.json()["mapping"] is None

    second_claim_response = client_b.post(
        "/api/workbench/claim-next",
        data={"category": "ChatGPT", "target_tag_id": str(platform_tag.id)},
    )
    second_claim = second_claim_response.json()["mapping"]

    assert second_claim_response.status_code == 200
    assert second_claim["id"] == second.id
    assert store.get_by_id(second.id).status == "in_progress"
    assert store.get_by_id(second.id).claimed_by
    assert store.get_by_id(first.id).claimed_by != store.get_by_id(second.id).claimed_by

    dashboard_response = client_a.get("/admin")
    assert dashboard_response.status_code == 200
    assert dashboard_response.text.count("注册中") == 2

    assert client_a.get("/api/workbench/current?category=ChatGPT").json()["mapping"]["id"] == first.id
    assert client_b.get("/api/workbench/current?category=ChatGPT").json()["mapping"]["id"] == second.id

    b_skip_first_response = client_b.post(
        "/api/workbench/current/skip",
        data={
            "mapping_id": str(first.id),
            "category": "ChatGPT",
            "target_tag_id": str(platform_tag.id),
        },
    )

    assert b_skip_first_response.status_code == 409
    assert b_skip_first_response.json()["message"] == "这个邮箱不是当前工作台领取的"
    assert store.get_by_id(first.id).status == "in_progress"

    fake_cloudmail.messages = [
        CloudMailMessage(
            email_id=31,
            send_email="noreply@tm.openai.com",
            send_name="OpenAI",
            subject="Your code is 313131",
            to_email=first.recipient_email,
            to_name="",
            create_time="2099-01-01 00:01:00",
            type=0,
            content="<p>313131</p>",
            text="313131",
            is_del=0,
        )
    ]

    complete_first_response = client_a.post(
        "/api/workbench/current/mark-used",
        data={
            "mapping_id": str(first.id),
            "category": "ChatGPT",
            "target_tag_id": str(platform_tag.id),
        },
    )
    complete_first_payload = complete_first_response.json()

    assert complete_first_response.status_code == 200
    assert complete_first_payload["completed"]["id"] == first.id
    assert complete_first_payload["completed"]["status"] == "idle"
    assert complete_first_payload["mapping"]["id"] == third.id
    assert store.get_by_id(first.id).claimed_by == ""
    assert store.get_by_id(second.id).status == "in_progress"
    assert store.get_by_id(third.id).status == "in_progress"
    assert store.count_verification_events(tag_id=platform_tag.id) == 1
    assert client_b.get("/api/workbench/current?category=ChatGPT").json()["mapping"]["id"] == second.id


def test_workbench_current_mailbox_clears_after_external_reset(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient()
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("ChatGPT", kind="business")
    platform_tag = store.create_tag("GPT", kind="service")
    first = store.create_mapping(
        recipient_email="first@example.com",
        query_email="first@example.com",
        access_key="first-key",
        category="ChatGPT",
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, store=store, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )
    workbench_response = client.get("/admin/workbench")
    claim_response = client.post(
        "/api/workbench/claim-next",
        data={"category": "ChatGPT", "target_tag_id": str(platform_tag.id)},
    )

    assert workbench_response.status_code == 200
    assert "当前领取已释放，请重新领取一个邮箱。" in workbench_response.text
    assert claim_response.json()["mapping"]["id"] == first.id

    store.reset_mapping_status(first.id)
    current_mailbox_response = client.get("/api/workbench/current/mailbox?category=ChatGPT")
    current_response = client.get("/api/workbench/current?category=ChatGPT")

    assert current_mailbox_response.status_code == 200
    assert current_mailbox_response.json()["mapping"] is None
    assert current_mailbox_response.json()["latest_code"] is None
    assert "emails" not in current_mailbox_response.json()
    assert current_response.json()["mapping"] is None



def test_mailbox_uses_compact_expandable_preview(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient(
        messages=[
            CloudMailMessage(
                email_id=1,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="你的临时 ChatGPT 登录代码",
                to_email="buyer@example.com",
                to_name="",
                create_time="2026-04-18 10:56:53",
                type=0,
                content="<div>第一行</div><div>第二行</div><div>第三行</div><div>第四行</div><div>第五行</div>",
                text="第一行\n第二行\n第三行\n第四行\n第五行",
                is_del=0,
            )
        ]
    )
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping(
        recipient_email="buyer@example.com",
        query_email="buyer@example.com",
        access_key="compact-key",
        label="demo",
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, store=store, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    response = client.get("/mailbox/compact-key")

    assert response.status_code == 200
    assert "line-clamp-4 whitespace-pre-wrap" in response.text
    assert "collapse collapse-arrow" in response.text
    assert "展开完整邮件内容" in response.text



def test_mailbox_supports_live_fragment_refresh_without_full_page_reload(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient(
        messages=[
            CloudMailMessage(
                email_id=1,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="旧验证码 111111",
                to_email="buyer@example.com",
                to_name="",
                create_time="2026-04-18 10:56:53",
                type=0,
                content="<div>旧验证码</div><div>111111</div>",
                text="旧验证码\n111111",
                is_del=0,
            )
        ]
    )
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping(
        recipient_email="buyer@example.com",
        query_email="buyer@example.com",
        access_key="live-key",
        label="demo",
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, store=store, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    page_response = client.get("/mailbox/live-key")

    assert page_response.status_code == 200
    assert 'data-mailbox-live-region' in page_response.text
    assert 'data-mailbox-loading-indicator' in page_response.text
    assert '正在获取新邮件' in page_response.text
    assert '/mailbox/live-key/fragment' in page_response.text
    assert 'setInterval' in page_response.text

    fake_cloudmail.messages = [
        CloudMailMessage(
            email_id=2,
            send_email="noreply@tm.openai.com",
            send_name="OpenAI",
            subject="新验证码 222222",
            to_email="buyer@example.com",
            to_name="",
            create_time="2026-04-18 10:57:53",
            type=0,
            content="<div>新验证码</div><div>222222</div>",
            text="新验证码\n222222",
            is_del=0,
        )
    ]

    fragment_response = client.get("/mailbox/live-key/fragment")

    assert fragment_response.status_code == 200
    assert "新验证码 222222" in fragment_response.text
    assert "222222" in fragment_response.text
    assert "旧验证码 111111" not in fragment_response.text



def test_mailbox_uses_configured_timezone_for_displayed_email_time(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient(
        messages=[
            CloudMailMessage(
                email_id=1,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="你的临时 ChatGPT 登录代码",
                to_email="buyer@example.com",
                to_name="",
                create_time="2026-04-18 10:56:53",
                type=0,
                content="<div>299929</div>",
                text="299929",
                is_del=0,
            )
        ]
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    store = KeyStore(tmp_path / "app.db")
    store.save_cloudmail_settings(
        base_url="https://mail.example.com",
        api_token="fixed-token",
        display_timezone="Asia/Shanghai",
    )
    store.create_mapping(
        recipient_email="buyer@example.com",
        query_email="buyer@example.com",
        access_key="tz-key",
        label="demo",
    )
    app = create_app(settings=settings, store=store, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    response = client.get("/mailbox/tz-key")

    assert response.status_code == 200
    assert "2026-04-18 18:56:53" in response.text
    assert "2026-04-18 10:56:53" not in response.text



def test_build_preview_removes_email_html_css_noise() -> None:
    preview = _build_preview(
        "",
        """
        <style>
          @font-face { font-family: \"Söhne\"; }
          .ExternalClass { width: 100%; }
        </style>
        <div>输入此临时验证码可以继续：138959</div>
        <div>如果并非你本人尝试创建 ChatGPT 帐户，请忽略此电子邮件。</div>
        """,
    )

    assert "138959" in preview
    assert "输入此临时验证码可以继续" in preview
    assert "@font-face" not in preview
    assert ".ExternalClass" not in preview


def test_build_preview_prefers_clean_html_over_textual_html_source() -> None:
    preview = _build_preview(
        "你的 ChatGPT 代码为 138959\n@font-face { font-family: Söhne; }\n.ExternalClass { width: 100%; }",
        "<div>你的 ChatGPT 代码为 138959</div><div>输入此临时验证码可以继续：138959</div>",
    )

    assert "你的 ChatGPT 代码为 138959" in preview
    assert "输入此临时验证码可以继续：138959" in preview
    assert "@font-face" not in preview
    assert ".ExternalClass" not in preview


def test_mailbox_filters_shared_query_mailbox_by_detected_original_recipient(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient(
        messages=[
            CloudMailMessage(
                email_id=1,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="code-for-other",
                to_email="openai@eve.ink",
                to_name="",
                create_time="2026-04-18 15:00:00",
                type=0,
                content="<div>收件人：other@example.com</div><div>111111</div>",
                text="收件人：other@example.com\n111111",
                is_del=0,
            ),
            CloudMailMessage(
                email_id=2,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="code-for-target",
                to_email="openai@eve.ink",
                to_name="",
                create_time="2026-04-18 15:01:00",
                type=0,
                content="<div>收件人：target@example.com</div><div>222222</div>",
                text="收件人：target@example.com\n222222",
                is_del=0,
            ),
        ]
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )
    client.post(
        "/admin/keys",
        data={
            "recipient_email": "target@example.com",
            "query_email": "openai@eve.ink",
            "access_key": "target-key",
            "label": "target",
        },
        follow_redirects=True,
    )

    response = client.get("/mailbox/target-key")

    assert response.status_code == 200
    assert "code-for-target" in response.text
    assert "222222" in response.text
    assert "code-for-other" not in response.text
    assert "111111" not in response.text


def test_mailbox_filters_shared_query_mailbox_by_recipient_json(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient(
        messages=[
            CloudMailMessage(
                email_id=1,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="code-for-other-json",
                to_email="openai@eve.ink",
                to_name="",
                create_time="2026-04-18 15:02:00",
                type=0,
                content="<div>330119</div>",
                text="330119",
                is_del=0,
                recipient='[{"address":"other@example.com","name":"other"}]',
            ),
            CloudMailMessage(
                email_id=2,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="code-for-target-json",
                to_email="openai@eve.ink",
                to_name="",
                create_time="2026-04-18 15:03:00",
                type=0,
                content="<div>220022</div>",
                text="220022",
                is_del=0,
                recipient='[{"address":"target@example.com","name":"target"}]',
            ),
        ]
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )
    client.post(
        "/admin/keys",
        data={
            "recipient_email": "target@example.com",
            "query_email": "openai@eve.ink",
            "access_key": "target-json-key",
            "label": "target-json",
        },
        follow_redirects=True,
    )

    response = client.get("/mailbox/target-json-key")

    assert response.status_code == 200
    assert "code-for-target-json" in response.text
    assert "220022" in response.text
    assert "code-for-other-json" not in response.text
    assert "330119" not in response.text



def test_mailbox_hides_shared_query_mailbox_messages_when_original_recipient_cannot_be_determined(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient(
        messages=[
            CloudMailMessage(
                email_id=1,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="mixed-mailbox-message",
                to_email="openai@eve.ink",
                to_name="",
                create_time="2026-04-18 15:02:00",
                type=0,
                content="<div>ChatGPT Log-in Code</div><div>330119</div>",
                text="ChatGPT Log-in Code\n330119",
                is_del=0,
            )
        ]
    )
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="secret",
        lookup_email_limit=5,
    )
    app = create_app(settings=settings, cloudmail_client=fake_cloudmail)
    client = TestClient(app)

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123"},
        follow_redirects=True,
    )
    client.post(
        "/admin/keys",
        data={
            "recipient_email": "target@example.com",
            "query_email": "openai@eve.ink",
            "access_key": "target-key",
            "label": "target",
        },
        follow_redirects=True,
    )

    response = client.get("/mailbox/target-key")

    assert response.status_code == 200
    assert "mixed-mailbox-message" not in response.text
    assert "330119" not in response.text
    assert "无法从 CloudMail 返回内容里识别原始收件人" in response.text


def test_external_api_requires_basic_auth_and_client_id(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping(recipient_email="first@example.com", category="未使用")
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    client = TestClient(create_app(settings=settings, store=store, cloudmail_client=FakeCloudMailClient()))

    unauthorized = client.get("/api/v1/categories")
    wrong_password = client.get("/api/v1/categories", auth=("admin", "wrong"))

    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"].startswith("Basic ")
    assert unauthorized.headers["cache-control"] == "no-store"
    assert wrong_password.status_code == 401

    categories_response = client.get("/api/v1/categories", auth=("admin", "pass123"))
    categories = categories_response.json()["categories"]
    assert categories_response.status_code == 200
    assert categories_response.headers["cache-control"] == "no-store"
    assert categories == [
        {
            "id": store.get_category_id("未使用"),
            "name": "未使用",
            "count": 1,
        }
    ]

    missing_client_id = client.post(
        "/api/v1/workbench/claim-next",
        auth=("admin", "pass123"),
        json={
            "category_id": categories[0]["id"],
            "target_tag_id": categories[0]["id"],
        },
    )
    unknown_category = client.post(
        "/api/v1/workbench/claim-next",
        auth=("admin", "pass123"),
        headers={"X-Client-ID": "worker-a"},
        json={"category_id": 999999, "target_tag_id": categories[0]["id"]},
    )

    assert missing_client_id.status_code == 400
    assert missing_client_id.json()["error"]["code"] == "client_id_required"
    assert unknown_category.status_code == 404
    assert unknown_category.json()["error"]["code"] == "source_tag_not_found"


def test_external_api_claims_by_category_returns_full_mail_and_isolates_clients(tmp_path) -> None:
    fake_cloudmail = FakeCloudMailClient(
        messages=[
            CloudMailMessage(
                email_id=20,
                send_email="other@example.com",
                send_name="Other",
                subject="Other code 998877",
                to_email="shared@example.com",
                to_name="",
                    create_time="2099-01-01 00:02:00",
                type=0,
                content="<p>Original Recipient: other@example.com</p><p>998877</p>",
                text="Original Recipient: other@example.com\n998877",
                is_del=0,
            ),
            CloudMailMessage(
                email_id=19,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="Your verification code is 112233",
                to_email="shared@example.com",
                to_name="First",
                    create_time="2099-01-01 00:01:00",
                type=0,
                content="<p>Original Recipient: first@example.com</p><strong>112233</strong>",
                text="Original Recipient: first@example.com\nYour verification code is 112233",
                is_del=0,
            ),
        ]
    )
    queued_messages = list(fake_cloudmail.messages)
    fake_cloudmail.messages = []
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("未使用", kind="business")
    platform_tag = store.create_tag("GPT", kind="service")
    first = store.create_mapping(
        recipient_email="first@example.com",
        query_email="shared@example.com",
        access_key="first-key",
        category="未使用",
    )
    second = store.create_mapping(
        recipient_email="second@example.com",
        access_key="second-key",
        category="未使用",
    )
    third = store.create_mapping(
        recipient_email="third@example.com",
        access_key="third-key",
        category="未使用",
    )
    store.create_mapping(recipient_email="waste@example.com", category="gpt废号")
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
        lookup_email_limit=5,
    )
    client = TestClient(create_app(settings=settings, store=store, cloudmail_client=fake_cloudmail))
    auth = ("admin", "pass123")
    worker_a = {"X-Client-ID": "worker-a"}
    worker_b = {"X-Client-ID": "worker-b"}
    source_category_id = store.get_category_id("未使用")
    assert source_category_id is not None

    first_claim = client.post(
        "/api/v1/workbench/claim-next",
        auth=auth,
        headers=worker_a,
        json={"category_id": source_category_id, "target_tag_id": platform_tag.id},
    )
    first_payload = first_claim.json()

    assert first_claim.status_code == 200
    assert first_payload["mapping"]["id"] == first.id
    assert first_payload["mapping"]["registration_email"] == "first@example.com"
    assert first_payload["mapping"]["category_id"] == source_category_id
    assert "access_key" not in first_payload["mapping"]
    assert "query_email" not in first_payload["mapping"]
    assert first_payload["registration_email"] == "first@example.com"
    fake_cloudmail.messages = queued_messages
    first_payload = client.get(
        "/api/v1/workbench/current",
        auth=auth,
        headers=worker_a,
    ).json()
    assert first_payload["latest_code"] == "112233"
    assert first_payload["latest_email"]["email_id"] == 19
    assert first_payload["latest_email"]["subject"] == "Your verification code is 112233"
    assert first_payload["latest_email"]["content"].endswith("<strong>112233</strong>")
    assert first_payload["latest_email"]["text"].endswith("Your verification code is 112233")
    assert first_payload["latest_email"]["detected_recipients"] == ["first@example.com"]

    repeated_claim = client.post(
        "/api/v1/workbench/claim-next",
        auth=auth,
        headers=worker_a,
        json={"category_id": source_category_id, "target_tag_id": platform_tag.id},
    )
    second_client_claim = client.post(
        "/api/v1/workbench/claim-next",
        auth=auth,
        headers=worker_b,
        json={"category_id": source_category_id, "target_tag_id": platform_tag.id},
    )
    assert repeated_claim.json()["mapping"]["id"] == first.id
    assert repeated_claim.json()["message"] == "已恢复当前领取的邮箱"
    assert second_client_claim.json()["mapping"]["id"] == second.id
    assert store.get_by_id(first.id).claimed_by != store.get_by_id(second.id).claimed_by

    current_a = client.get("/api/v1/workbench/current", auth=auth, headers=worker_a)
    current_b = client.get("/api/v1/workbench/current", auth=auth, headers=worker_b)
    assert current_a.json()["mapping"]["id"] == first.id
    assert current_b.json()["mapping"]["id"] == second.id

    completed = client.post(
        "/api/v1/workbench/complete",
        auth=auth,
        headers=worker_a,
        json={
            "mapping_id": first.id,
            "category_id": source_category_id,
        },
    )
    completed_payload = completed.json()
    assert completed.status_code == 200
    assert completed_payload["completed"]["id"] == first.id
    assert completed_payload["completed"]["category_id"] == source_category_id
    assert completed_payload["mapping"]["id"] == third.id
    assert store.get_by_id(first.id).category == "未使用"
    assert store.count_verification_events(tag_id=platform_tag.id) == 1
    assert client.get("/api/v1/workbench/current", auth=auth, headers=worker_b).json()["mapping"]["id"] == second.id

    skipped = client.post(
        "/api/v1/workbench/skip-current",
        auth=auth,
        headers=worker_b,
        json={
            "mapping_id": second.id,
            "category_id": source_category_id,
        },
    )
    assert skipped.status_code == 200
    assert skipped.json()["skipped"]["id"] == second.id
    assert skipped.json()["mapping"] is None
    assert store.get_by_id(second.id).status == "idle"
    assert client.get("/api/v1/workbench/current", auth=auth, headers=worker_a).json()["mapping"]["id"] == third.id
