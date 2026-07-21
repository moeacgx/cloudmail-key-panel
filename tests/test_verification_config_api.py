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


def test_ai_config_test_reuses_saved_api_key_and_masks_upstream_failure(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer saved-secret"
        return httpx.Response(401, text="upstream-secret-error")

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

    assert response.status_code == 502
    assert response.json()["error"] == "AI 接口请求失败（HTTP 401）"
    assert "saved-secret" not in response.text
    assert "upstream-secret-error" not in response.text
    saved = store.get_verification_extraction_settings()
    assert saved.base_url == "https://old.example.com/v1"
    assert saved.api_key == "saved-secret"
    assert saved.model == "old-model"


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
