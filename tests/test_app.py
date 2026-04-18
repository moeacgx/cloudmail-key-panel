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
    assert fake_cloudmail.calls == [("cranes_solute.1o@icloud.com", 5)]
