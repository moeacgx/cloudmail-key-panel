from fastapi.testclient import TestClient

from app.cloudmail import CloudMailMessage
from app.main import create_app
from app.settings import AppSettings


class FakeCloudMailClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def fetch_recent_emails(self, recipient_email: str, limit: int = 10) -> list[CloudMailMessage]:
        self.calls.append((recipient_email, limit))
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
            "query_email": "openai@eve.ink",
            "access_key": "demo-key-001",
            "label": "order-1",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert "demo-key-001" in create_response.text
    assert "cranes_solute.1o@icloud.com" in create_response.text

    lookup_response = client.post(
        "/lookup",
        data={"access_key": "demo-key-001"},
        follow_redirects=True,
    )

    assert lookup_response.status_code == 200
    assert "330119" in lookup_response.text
    assert "noreply@tm.openai.com" in lookup_response.text
    assert "cranes_solute.1o@icloud.com" in lookup_response.text
    assert fake_cloudmail.calls == [("openai@eve.ink", 5)]



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
            "default_query_email": "openai@eve.ink",
        },
        follow_redirects=True,
    )

    assert save_response.status_code == 200
    assert "CloudMail 配置已保存" in save_response.text
    assert "https://mail.boxmoe.eu.org/" in save_response.text
    assert 'name="query_email" value="openai@eve.ink"' in save_response.text

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
    assert fake_factory.calls == [("openai@eve.ink", 5)]
    assert fake_factory.configs[-1].base_url == "https://mail.boxmoe.eu.org/"
    assert fake_factory.configs[-1].api_token == "fixed-token-123"


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
