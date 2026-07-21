from app.settings import AppSettings


def test_settings_from_env_loads_local_dotenv_file(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_NAME=Loaded From Dotenv\n"
        "APP_ADMIN_USERNAME=dotenv-admin\n"
        "LOOKUP_EMAIL_LIMIT=7\n"
        "REDEMPTION_SKIP_LIMIT=4\n"
        "REDEMPTION_SKIP_COOLDOWN_MINUTES=25\n"
        "PUBLIC_RECENT_MAILBOX_LIMIT=30\n"
        "VERIFICATION_EXTRACTION_MODE=fallback\n"
        'VERIFICATION_CODE_PATTERNS=["token=([0-9]{6})", "code=([A-Z0-9-]+)"]\n'
        "VERIFICATION_AI_BASE_URL=https://ai.example.com/v1\n"
        "VERIFICATION_AI_API_KEY=test-key\n"
        "VERIFICATION_AI_MODEL=extract-model\n"
        "VERIFICATION_AI_TIMEOUT_SECONDS=6\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("APP_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("LOOKUP_EMAIL_LIMIT", raising=False)
    monkeypatch.delenv("REDEMPTION_SKIP_LIMIT", raising=False)
    monkeypatch.delenv("REDEMPTION_SKIP_COOLDOWN_MINUTES", raising=False)
    monkeypatch.delenv("PUBLIC_RECENT_MAILBOX_LIMIT", raising=False)
    monkeypatch.delenv("VERIFICATION_EXTRACTION_MODE", raising=False)
    monkeypatch.delenv("VERIFICATION_CODE_PATTERNS", raising=False)
    monkeypatch.delenv("VERIFICATION_AI_BASE_URL", raising=False)
    monkeypatch.delenv("VERIFICATION_AI_API_KEY", raising=False)
    monkeypatch.delenv("VERIFICATION_AI_MODEL", raising=False)
    monkeypatch.delenv("VERIFICATION_AI_TIMEOUT_SECONDS", raising=False)

    settings = AppSettings.from_env()

    assert settings.app_name == "Loaded From Dotenv"
    assert settings.app_admin_username == "dotenv-admin"
    assert settings.lookup_email_limit == 7
    assert settings.redemption_skip_limit == 4
    assert settings.redemption_skip_cooldown_minutes == 25
    assert settings.public_recent_mailbox_limit == 30
    assert settings.verification_extraction_mode == "fallback"
    assert settings.verification_code_patterns == (
        "token=([0-9]{6})",
        "code=([A-Z0-9-]+)",
    )
    assert settings.verification_ai_base_url == "https://ai.example.com/v1"
    assert settings.verification_ai_api_key == "test-key"
    assert settings.verification_ai_model == "extract-model"
    assert settings.verification_ai_timeout_seconds == 6
