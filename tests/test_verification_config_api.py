from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import AppSettings
from app.store import KeyStore


def _create_test_client(tmp_path, handler) -> tuple[TestClient, KeyStore]:
    settings = AppSettings(
        app_secret_key="test-secret",
        app_admin_username="admin",
        app_admin_password="pass123",
        database_path=str(tmp_path / "app.db"),
    )
    store = KeyStore(tmp_path / "app.db")
    app = create_app(
        settings=settings,
        store=store,
        verification_ai_transport=httpx.MockTransport(handler),
    )
    return TestClient(app), store


def test_admin_can_test_unsaved_ai_config_without_persisting_it(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "https://ai.example.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer unsaved-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert "TEST-7Q9" in payload["messages"][1]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"code":"TEST-7Q9"}'}}]},
        )

    client, store = _create_test_client(tmp_path, handler)
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})

    response = client.post(
        "/admin/verification-extraction/test",
        data={
            "base_url": "https://ai.example.com/v1",
            "api_key": "unsaved-secret",
            "model": "test-model",
            "timeout_seconds": "5",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "接口测试成功" in response.json()["message"]
    assert response.headers["cache-control"] == "no-store"
    assert len(requests) == 1
    saved = store.get_verification_extraction_settings()
    assert saved.base_url == ""
    assert saved.api_key == ""
    assert saved.model == ""


def test_ai_config_test_reuses_saved_api_key_on_same_origin(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer saved-secret"
        return httpx.Response(401, text="upstream-secret-error")

    client, store = _create_test_client(tmp_path, handler)
    store.save_verification_extraction_settings(
        base_url="https://new.example.com/legacy",
        api_key="saved-secret",
        model="old-model",
    )
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})

    response = client.post(
        "/admin/verification-extraction/test",
        data={
            "base_url": "https://new.example.com/v1",
            "api_key": "",
            "model": "new-model",
            "timeout_seconds": "5",
        },
    )

    assert response.status_code == 424
    assert response.json()["error"] == (
        "AI 接口拒绝了当前密钥（HTTP 401），请重新填写该接口对应的 API Key"
    )
    assert "saved-secret" not in response.text
    assert "upstream-secret-error" not in response.text
    saved = store.get_verification_extraction_settings()
    assert saved.base_url == "https://new.example.com/legacy"
    assert saved.api_key == "saved-secret"
    assert saved.model == "old-model"


def test_ai_config_test_does_not_send_saved_key_to_changed_origin(tmp_path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    client, store = _create_test_client(tmp_path, handler)
    store.save_verification_extraction_settings(
        base_url="https://old.example.com/v1",
        api_key="saved-secret",
        model="old-model",
    )
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})

    response = client.post(
        "/admin/verification-extraction/test",
        data={
            "base_url": "https://new.example.com/v1",
            "api_key": "",
            "model": "new-model",
            "timeout_seconds": "5",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "接口域名已改变，请重新填写这个接口对应的 API Key"
    assert calls == 0


def test_ai_config_test_requires_admin_and_complete_config(tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client, _ = _create_test_client(tmp_path, handler)
    payload = {
        "base_url": "https://ai.example.com/v1",
        "api_key": "secret",
        "model": "test-model",
        "timeout_seconds": "5",
    }

    unauthorized = client.post("/admin/verification-extraction/test", data=payload)
    assert unauthorized.status_code == 401

    client.post("/admin/login", data={"username": "admin", "password": "pass123"})
    incomplete = client.post(
        "/admin/verification-extraction/test",
        data={**payload, "model": ""},
    )
    assert incomplete.status_code == 400
    assert incomplete.json()["error"] == "AI 接口地址、密钥和模型必须同时填写"

    clearing = client.post(
        "/admin/verification-extraction/test",
        data={**payload, "clear_api_key": "true"},
    )
    assert clearing.status_code == 400
    assert clearing.json()["error"] == "已勾选清除密钥，无法测试 AI 接口"
    assert calls == 0


def test_ai_config_test_explains_non_json_endpoint(tmp_path) -> None:
    client, _ = _create_test_client(
        tmp_path,
        lambda _request: httpx.Response(200, text="<html>website homepage</html>"),
    )
    client.post("/admin/login", data={"username": "admin", "password": "pass123"})

    response = client.post(
        "/admin/verification-extraction/test",
        data={
            "base_url": "https://website.example.com",
            "api_key": "secret",
            "model": "test-model",
            "timeout_seconds": "5",
        },
    )

    assert response.status_code == 424
    assert response.json()["error"] == (
        "AI 接口返回的不是 JSON，请确认填写的是 /v1 接口地址而不是网站首页"
    )
