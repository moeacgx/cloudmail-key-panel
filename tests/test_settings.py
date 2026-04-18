from app.settings import AppSettings


def test_settings_from_env_loads_local_dotenv_file(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_NAME=Loaded From Dotenv\n"
        "APP_ADMIN_USERNAME=dotenv-admin\n"
        "LOOKUP_EMAIL_LIMIT=7\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("APP_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("LOOKUP_EMAIL_LIMIT", raising=False)

    settings = AppSettings.from_env()

    assert settings.app_name == "Loaded From Dotenv"
    assert settings.app_admin_username == "dotenv-admin"
    assert settings.lookup_email_limit == 7
