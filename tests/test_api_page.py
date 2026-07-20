from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


def _settings(tmp_path) -> AppSettings:
    return AppSettings(
        app_secret_key="test-secret-never-render",
        app_admin_username="admin",
        app_admin_password="pass123-never-render",
        database_path=str(tmp_path / "app.db"),
        cloudmail_base_url="https://mail.example.com",
        cloudmail_admin_email="admin@example.com",
        cloudmail_admin_password="cloudmail-secret-never-render",
        lookup_email_limit=5,
    )


def _login(client: TestClient) -> None:
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "pass123-never-render"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_admin_api_page_requires_login_and_is_linked_from_admin_pages(tmp_path) -> None:
    client = TestClient(create_app(settings=_settings(tmp_path)))

    unauthorized = client.get("/admin/api", follow_redirects=False)

    assert unauthorized.status_code == 303
    assert unauthorized.headers["location"] == "/admin/login"

    _login(client)
    dashboard = client.get("/admin")
    workbench = client.get("/admin/workbench")

    assert dashboard.status_code == 200
    assert workbench.status_code == 200
    assert 'href="/admin/api"' in dashboard.text
    assert 'href="/admin/api"' in workbench.text
    assert "API 文档与调试" in dashboard.text
    assert "API 文档与调试" in workbench.text


def test_admin_api_page_documents_endpoints_and_contains_safe_live_debugger(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping(recipient_email="first@example.com", category="未使用")
    store.create_mapping(recipient_email="second@example.com", category="未使用")
    store.create_mapping(recipient_email="used@example.com", category="gpt废号")
    client = TestClient(create_app(settings=_settings(tmp_path), store=store))
    _login(client)

    response = client.get("/admin/api")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "API 文档与调试" in response.text
    assert "HTTP Basic" in response.text
    assert "X-Client-ID" in response.text
    assert "最新验证码和最新完整邮件" in response.text

    # 页面应完整列出当前公开的 v1 接口及主要返回字段。
    for endpoint in (
        "/api/v1/tags",
        "/api/v1/workbench/claim-next",
        "/api/v1/workbench/current",
        "/api/v1/workbench/complete",
        "/api/v1/workbench/skip-current",
    ):
        assert endpoint in response.text
    for field in (
        "registration_email",
        "category_id",
        "latest_code",
        "latest_email.content",
        "latest_email.text",
        "alias_use_limit",
    ):
        assert field in response.text

    assert f'value="{store.get_category_id("未使用")}"' in response.text
    assert "未使用（2）" in response.text
    assert f'value="{store.get_category_id("gpt废号")}"' in response.text

    # 调试台必须能构造并发送真实请求，同时明确提示会修改实际状态。
    assert 'data-api-debugger' in response.text
    assert 'data-api-action="tags"' in response.text
    assert 'data-api-action="current"' in response.text
    assert 'data-api-action="claim"' in response.text
    assert 'data-api-action="skip"' in response.text
    assert 'data-api-action="complete"' in response.text
    assert "await fetch(`${apiBase}${request.path}`" in response.text
    assert "Authorization: `Basic ${encodeBasicCredentials" in response.text
    assert "headers['X-Client-ID'] = clientId" in response.text
    assert 'data-api-base="/api/v1"' in response.text
    assert "credentials: 'omit'" in response.text
    assert 'value="panel-debug"' in response.text
    assert '<option value="" selected disabled>请选择来源标签</option>' in response.text
    assert '<option value="" selected disabled>请选择平台标签</option>' in response.text
    assert "complete_category_id" not in response.text
    assert "target_site" not in response.text
    assert "window.confirm(" in response.text
    assert "不是模拟数据" in response.text
    assert "cURL 演示" in response.text
    assert "调试响应" in response.text
    assert "ADMIN_PASSWORD" in response.text

    # 服务端只能预填账号，任何后台或 CloudMail 密码都不能进入 HTML/JavaScript。
    assert 'value="admin"' in response.text
    assert 'type="password"' in response.text
    assert "pass123-never-render" not in response.text
    assert "cloudmail-secret-never-render" not in response.text
    assert "test-secret-never-render" not in response.text
