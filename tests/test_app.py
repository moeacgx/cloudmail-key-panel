from fastapi.testclient import TestClient

from app.cloudmail import CloudMailMessage
from app.main import _build_preview, create_app
from app.settings import AppSettings
from app.store import KeyStore


class FakeCloudMailClient:
    def __init__(self, messages: list[CloudMailMessage] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.messages = messages or [
            CloudMailMessage(
                email_id=1,
                send_email="noreply@tm.openai.com",
                send_name="OpenAI",
                subject="Your ChatGPT code is 330119",
                to_email="",
                to_name="buyer",
                create_time="2026-04-18 14:59:00",
                type=0,
                content="<div>ChatGPT Log-in Code</div><div>330119</div>",
                text="ChatGPT Log-in Code\n330119",
                is_del=0,
            )
        ]

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
                        create_time="2026-04-18 14:59:00",
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
        },
        follow_redirects=True,
    )

    assert save_response.status_code == 200
    assert "CloudMail 配置已保存" in save_response.text
    assert "https://mail.boxmoe.eu.org/" in save_response.text
    assert 'name="query_email" value="openai@eve.ink"' in save_response.text
    assert 'name="internal_admin_email" value="admin@example.com"' in save_response.text
    assert 'name="recent_email_limit" min="1" step="1" value="3"' in save_response.text
    assert 'id="cloudmail-config-dialog"' in save_response.text
    assert 'data-cloudmail-config-trigger' in save_response.text
    assert "编辑 CloudMail 配置" in save_response.text

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

    assert "site-header centered-header" in home_response.text


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
    assert "编辑这个 Key" in create_response.text
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
    assert "compact-action-button" in filter_response.text

    assert page_two_response.status_code == 200
    assert "buyer-key-02" in page_two_response.text
    assert "buyer-key-01" in page_two_response.text
    assert "buyer-key-12" not in page_two_response.text
    assert "第 2 / 2 页" in page_two_response.text



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
    assert 'textarea name="recipient_email"' in response.text
    assert "分类" in response.text
    assert store.count_mappings() == 3
    assert all(mapping.category == "ChatGPT" for mapping in store.list_mappings())
    assert {mapping.recipient_email for mapping in store.list_mappings()} == {
        "buyer1@example.com",
        "buyer2@example.com",
        "buyer3@example.com",
    }
    assert all(mapping.access_key for mapping in store.list_mappings())



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
    assert 'class="preview-snippet"' in response.text
    assert 'class="mail-preview-details"' in response.text
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
