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
