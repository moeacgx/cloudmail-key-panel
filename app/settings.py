from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(slots=True)
class AppSettings:
    app_name: str = "CloudMail Key Panel"
    app_secret_key: str = "change-me"
    app_admin_username: str = "admin"
    app_admin_password: str = "change-me"
    database_path: str = "./data/app.db"
    cloudmail_base_url: str = "https://mail.example.com"
    cloudmail_admin_email: str = ""
    cloudmail_admin_password: str = ""
    cloudmail_internal_admin_email: str = ""
    cloudmail_internal_admin_password: str = ""
    cloudmail_api_token: str | None = None
    lookup_email_limit: int = 10

    @classmethod
    def from_env(cls) -> "AppSettings":
        load_dotenv(dotenv_path=".env", override=False)
        defaults = cls()
        return cls(
            app_name=os.getenv("APP_NAME", defaults.app_name),
            app_secret_key=os.getenv("APP_SECRET_KEY", defaults.app_secret_key),
            app_admin_username=os.getenv("APP_ADMIN_USERNAME", defaults.app_admin_username),
            app_admin_password=os.getenv("APP_ADMIN_PASSWORD", defaults.app_admin_password),
            database_path=os.getenv("DATABASE_PATH", defaults.database_path),
            cloudmail_base_url=os.getenv("CLOUDMAIL_BASE_URL", defaults.cloudmail_base_url),
            cloudmail_admin_email=os.getenv("CLOUDMAIL_ADMIN_EMAIL", defaults.cloudmail_admin_email),
            cloudmail_admin_password=os.getenv("CLOUDMAIL_ADMIN_PASSWORD", defaults.cloudmail_admin_password),
            cloudmail_internal_admin_email=os.getenv("CLOUDMAIL_INTERNAL_ADMIN_EMAIL", defaults.cloudmail_internal_admin_email),
            cloudmail_internal_admin_password=os.getenv("CLOUDMAIL_INTERNAL_ADMIN_PASSWORD", defaults.cloudmail_internal_admin_password),
            cloudmail_api_token=os.getenv("CLOUDMAIL_API_TOKEN") or None,
            lookup_email_limit=int(os.getenv("LOOKUP_EMAIL_LIMIT", str(defaults.lookup_email_limit))),
        )
