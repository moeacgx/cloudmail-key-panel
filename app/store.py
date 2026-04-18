from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True)
class AccessMapping:
    id: int
    recipient_email: str
    access_key: str
    label: str
    created_at: str


@dataclass(slots=True)
class CloudMailSettingsRecord:
    base_url: str
    api_token: str
    updated_at: str


class KeyStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_mapping(self, recipient_email: str, access_key: str | None = None, label: str = "") -> AccessMapping:
        normalized_email = recipient_email.strip().lower()
        normalized_key = (access_key or self._generate_key()).strip()
        normalized_label = label.strip()

        if not normalized_email:
            raise ValueError("recipient_email is required")
        if not normalized_key:
            raise ValueError("access_key is required")

        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO access_mappings (recipient_email, access_key, label, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (normalized_email, normalized_key, normalized_label, created_at),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("access_key already exists") from exc

        return AccessMapping(
            id=int(cursor.lastrowid),
            recipient_email=normalized_email,
            access_key=normalized_key,
            label=normalized_label,
            created_at=created_at,
        )

    def get_by_key(self, access_key: str) -> AccessMapping | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, recipient_email, access_key, label, created_at FROM access_mappings WHERE access_key = ?",
                (access_key.strip(),),
            ).fetchone()

        return self._row_to_mapping(row)

    def list_mappings(self) -> list[AccessMapping]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, recipient_email, access_key, label, created_at FROM access_mappings ORDER BY id DESC"
            ).fetchall()

        return [mapping for row in rows if (mapping := self._row_to_mapping(row)) is not None]

    def save_cloudmail_settings(self, base_url: str, api_token: str) -> CloudMailSettingsRecord:
        normalized_base_url = base_url.strip()
        normalized_api_token = api_token.strip()

        if not normalized_base_url:
            raise ValueError("base_url is required")
        if not normalized_api_token:
            raise ValueError("api_token is required")

        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                [
                    ("cloudmail_base_url", normalized_base_url, updated_at),
                    ("cloudmail_api_token", normalized_api_token, updated_at),
                ],
            )
            connection.commit()

        return CloudMailSettingsRecord(
            base_url=normalized_base_url,
            api_token=normalized_api_token,
            updated_at=updated_at,
        )

    def get_cloudmail_settings(
        self,
        default_base_url: str = "",
        default_api_token: str = "",
    ) -> CloudMailSettingsRecord:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT key, value, updated_at FROM app_settings WHERE key IN ('cloudmail_base_url', 'cloudmail_api_token')"
            ).fetchall()

        values = {
            "cloudmail_base_url": default_base_url,
            "cloudmail_api_token": default_api_token,
        }
        updated_at = ""

        for row in rows:
            values[row["key"]] = row["value"]
            updated_at = max(updated_at, row["updated_at"])

        return CloudMailSettingsRecord(
            base_url=values["cloudmail_base_url"],
            api_token=values["cloudmail_api_token"],
            updated_at=updated_at,
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS access_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient_email TEXT NOT NULL,
                    access_key TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _generate_key() -> str:
        return secrets.token_urlsafe(12)

    @staticmethod
    def _row_to_mapping(row: sqlite3.Row | None) -> AccessMapping | None:
        if row is None:
            return None
        return AccessMapping(
            id=row["id"],
            recipient_email=row["recipient_email"],
            access_key=row["access_key"],
            label=row["label"],
            created_at=row["created_at"],
        )
