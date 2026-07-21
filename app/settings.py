from __future__ import annotations

import json
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
    display_timezone: str = "UTC"
    redemption_session_hours: int = 24
    redemption_claim_minutes: int = 30
    redemption_skip_limit: int = 3
    redemption_skip_cooldown_minutes: int = 15
    public_recent_mailbox_limit: int = 20
    verification_extraction_mode: str = "off"
    verification_code_patterns: tuple[str, ...] = ()
    verification_ai_base_url: str = ""
    verification_ai_api_key: str = ""
    verification_ai_model: str = ""
    verification_ai_timeout_seconds: int = 10

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
            display_timezone=os.getenv("APP_DISPLAY_TIMEZONE", defaults.display_timezone),
            redemption_session_hours=int(
                os.getenv("REDEMPTION_SESSION_HOURS", str(defaults.redemption_session_hours))
            ),
            redemption_claim_minutes=int(
                os.getenv("REDEMPTION_CLAIM_MINUTES", str(defaults.redemption_claim_minutes))
            ),
            redemption_skip_limit=int(
                os.getenv("REDEMPTION_SKIP_LIMIT", str(defaults.redemption_skip_limit))
            ),
            redemption_skip_cooldown_minutes=int(
                os.getenv(
                    "REDEMPTION_SKIP_COOLDOWN_MINUTES",
                    str(defaults.redemption_skip_cooldown_minutes),
                )
            ),
            public_recent_mailbox_limit=int(
                os.getenv("PUBLIC_RECENT_MAILBOX_LIMIT", str(defaults.public_recent_mailbox_limit))
            ),
            verification_extraction_mode=os.getenv(
                "VERIFICATION_EXTRACTION_MODE", defaults.verification_extraction_mode
            ),
            verification_code_patterns=_parse_pattern_list(
                os.getenv("VERIFICATION_CODE_PATTERNS", "")
            ),
            verification_ai_base_url=os.getenv(
                "VERIFICATION_AI_BASE_URL", defaults.verification_ai_base_url
            ),
            verification_ai_api_key=os.getenv(
                "VERIFICATION_AI_API_KEY", defaults.verification_ai_api_key
            ),
            verification_ai_model=os.getenv(
                "VERIFICATION_AI_MODEL", defaults.verification_ai_model
            ),
            verification_ai_timeout_seconds=int(
                os.getenv(
                    "VERIFICATION_AI_TIMEOUT_SECONDS",
                    str(defaults.verification_ai_timeout_seconds),
                )
            ),
        )


def _parse_pattern_list(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("VERIFICATION_CODE_PATTERNS must be a JSON array") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ValueError("VERIFICATION_CODE_PATTERNS must be a JSON array of strings")
    return tuple(item.strip() for item in parsed if item.strip())
