from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VALID_MAPPING_STATUSES = {"idle", "in_progress"}
LEGACY_MAPPING_STATUS_ALIASES = {
    "unused": "idle",
    "used": "idle",
    "skipped": "idle",
    "failed": "idle",
}
MAPPING_STATUS_LABELS = {
    "idle": "空闲",
    "in_progress": "注册中",
}


@dataclass(slots=True)
class AccessMapping:
    id: int
    recipient_email: str
    query_email: str
    access_key: str
    label: str
    category: str
    created_at: str
    status: str
    claimed_at: str
    claimed_by: str
    used_at: str
    last_seen_email_id: int
    target_site: str


@dataclass(slots=True)
class CloudMailSettingsRecord:
    base_url: str
    api_token: str
    internal_admin_email: str
    internal_admin_password: str
    default_query_email: str
    recent_email_limit: int
    display_timezone: str
    updated_at: str


class KeyStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_mapping(
        self,
        recipient_email: str,
        query_email: str | None = None,
        access_key: str | None = None,
        label: str = "",
        category: str = "",
        status: str = "idle",
        target_site: str = "",
    ) -> AccessMapping:
        normalized_email = recipient_email.strip().lower()
        normalized_query_email = (query_email or normalized_email).strip().lower()
        normalized_key = (access_key or self._generate_key()).strip()
        normalized_label = label.strip()
        normalized_category = category.strip()
        normalized_status = self._normalize_status(status)
        normalized_target_site = target_site.strip()

        if not normalized_email:
            raise ValueError("recipient_email is required")
        if not normalized_query_email:
            raise ValueError("query_email is required")
        if not normalized_key:
            raise ValueError("access_key is required")

        created_at = self._now()

        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO access_mappings (
                        recipient_email,
                        query_email,
                        access_key,
                        label,
                        category,
                        created_at,
                        status,
                        claimed_at,
                        claimed_by,
                        used_at,
                        last_seen_email_id,
                        target_site
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '', 0, ?)
                    """,
                    (
                        normalized_email,
                        normalized_query_email,
                        normalized_key,
                        normalized_label,
                        normalized_category,
                        created_at,
                        normalized_status,
                        normalized_target_site,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("access_key already exists") from exc

        return AccessMapping(
            id=int(cursor.lastrowid),
            recipient_email=normalized_email,
            query_email=normalized_query_email,
            access_key=normalized_key,
            label=normalized_label,
            category=normalized_category,
            created_at=created_at,
            status=normalized_status,
            claimed_at="",
            claimed_by="",
            used_at="",
            last_seen_email_id=0,
            target_site=normalized_target_site,
        )

    def update_mapping(
        self,
        mapping_id: int,
        recipient_email: str,
        query_email: str | None,
        access_key: str,
        label: str = "",
        category: str = "",
    ) -> AccessMapping:
        normalized_email = recipient_email.strip().lower()
        normalized_query_email = (query_email or normalized_email).strip().lower()
        normalized_key = access_key.strip()
        normalized_label = label.strip()
        normalized_category = category.strip()

        if not normalized_email:
            raise ValueError("recipient_email is required")
        if not normalized_query_email:
            raise ValueError("query_email is required")
        if not normalized_key:
            raise ValueError("access_key is required")

        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE access_mappings
                    SET recipient_email = ?, query_email = ?, access_key = ?, label = ?, category = ?
                    WHERE id = ?
                    """,
                    (
                        normalized_email,
                        normalized_query_email,
                        normalized_key,
                        normalized_label,
                        normalized_category,
                        mapping_id,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("access_key already exists") from exc

        if cursor.rowcount == 0:
            raise ValueError("mapping not found")

        updated = self.get_by_id(mapping_id)
        if updated is None:
            raise ValueError("mapping not found")
        return updated

    def delete_mapping(self, mapping_id: int) -> None:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM access_mappings WHERE id = ?", (mapping_id,))
            connection.commit()

        if cursor.rowcount == 0:
            raise ValueError("mapping not found")

    def get_by_id(self, mapping_id: int) -> AccessMapping | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, recipient_email, query_email, access_key, label, category, created_at, status, claimed_at,
                       claimed_by,
                       used_at, last_seen_email_id, target_site
                FROM access_mappings
                WHERE id = ?
                """,
                (mapping_id,),
            ).fetchone()

        return self._row_to_mapping(row)

    def get_by_key(self, access_key: str) -> AccessMapping | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, recipient_email, query_email, access_key, label, category, created_at, status, claimed_at,
                       claimed_by,
                       used_at, last_seen_email_id, target_site
                FROM access_mappings
                WHERE access_key = ?
                """,
                (access_key.strip(),),
            ).fetchone()

        return self._row_to_mapping(row)

    def list_mappings(
        self,
        search_query: str = "",
        category_filter: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AccessMapping]:
        where_clause, params = self._build_mapping_filters(search_query, category_filter)
        query = """
            SELECT id, recipient_email, query_email, access_key, label, category, created_at, status, claimed_at,
                   claimed_by,
                   used_at, last_seen_email_id, target_site
            FROM access_mappings
        """
        if where_clause:
            query = f"{query} WHERE {where_clause}"
        query = f"{query} ORDER BY id DESC"
        if limit is not None:
            query = f"{query} LIMIT ? OFFSET ?"
            params.extend([max(int(limit), 1), max(int(offset), 0)])

        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()

        return [mapping for row in rows if (mapping := self._row_to_mapping(row)) is not None]

    def count_mappings(self, search_query: str = "", category_filter: str = "") -> int:
        where_clause, params = self._build_mapping_filters(search_query, category_filter)
        query = "SELECT COUNT(*) FROM access_mappings"
        if where_clause:
            query = f"{query} WHERE {where_clause}"

        with self._connect() as connection:
            return int(connection.execute(query, tuple(params)).fetchone()[0])

    def list_categories(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT category FROM access_mappings WHERE TRIM(category) != '' ORDER BY LOWER(category) ASC"
            ).fetchall()

        return [str(row[0]) for row in rows]

    def delete_mappings(self, mapping_ids: list[int]) -> int:
        normalized_ids = sorted({int(mapping_id) for mapping_id in mapping_ids})
        if not normalized_ids:
            raise ValueError("mapping_ids is required")

        placeholders = ", ".join("?" for _ in normalized_ids)
        with self._connect() as connection:
            cursor = connection.execute(f"DELETE FROM access_mappings WHERE id IN ({placeholders})", tuple(normalized_ids))
            connection.commit()

        return int(cursor.rowcount)

    def get_current_workbench_mapping(self, category_filter: str = "", claimed_by: str = "") -> AccessMapping | None:
        normalized_claimed_by = claimed_by.strip()
        where_clause = "status = ? AND claimed_by = ?"
        params: list[str] = ["in_progress", normalized_claimed_by]
        normalized_category = category_filter.strip().lower()
        if normalized_category:
            where_clause = f"{where_clause} AND LOWER(category) = ?"
            params.append(normalized_category)

        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT id, recipient_email, query_email, access_key, label, category, created_at, status, claimed_at,
                       claimed_by,
                       used_at, last_seen_email_id, target_site
                FROM access_mappings
                WHERE {where_clause}
                ORDER BY claimed_at DESC, id ASC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()

        return self._row_to_mapping(row)

    def claim_next_available_mapping(
        self,
        category_filter: str = "",
        target_site: str = "",
        claimed_by: str = "",
    ) -> AccessMapping | None:
        normalized_category = category_filter.strip().lower()
        normalized_target_site = target_site.strip()
        normalized_claimed_by = claimed_by.strip()
        claimed_at = self._now()

        if not normalized_claimed_by:
            raise ValueError("claimed_by is required")

        current = self.get_current_workbench_mapping(claimed_by=normalized_claimed_by)
        if current is not None:
            return current

        where_clause = "status = ?"
        params: list[str] = ["idle"]
        if normalized_category:
            where_clause = f"{where_clause} AND LOWER(category) = ?"
            params.append(normalized_category)

        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT id
                FROM access_mappings
                WHERE {where_clause}
                ORDER BY id ASC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
            if row is None:
                return None

            mapping_id = int(row["id"])
            cursor = connection.execute(
                """
                UPDATE access_mappings
                SET status = 'in_progress',
                    claimed_at = ?,
                    claimed_by = ?,
                    used_at = '',
                    target_site = CASE WHEN ? != '' THEN ? ELSE target_site END
                WHERE id = ? AND status = 'idle'
                """,
                (
                    claimed_at,
                    normalized_claimed_by,
                    normalized_target_site,
                    normalized_target_site,
                    mapping_id,
                ),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                return self.claim_next_available_mapping(
                    category_filter=category_filter,
                    target_site=target_site,
                    claimed_by=claimed_by,
                )
            connection.commit()

        return self.get_by_id(mapping_id)

    def update_mapping_status(
        self,
        mapping_id: int,
        status: str,
        target_site: str | None = None,
        claimed_by: str = "",
    ) -> AccessMapping:
        normalized_status = self._normalize_status(status)
        normalized_target_site = None if target_site is None else target_site.strip()
        normalized_claimed_by = claimed_by.strip()
        now = self._now()
        claimed_at_value = now if normalized_status == "in_progress" else None

        if normalized_status == "in_progress" and not normalized_claimed_by:
            raise ValueError("claimed_by is required")

        with self._connect() as connection:
            existing = connection.execute("SELECT id FROM access_mappings WHERE id = ?", (mapping_id,)).fetchone()
            if existing is None:
                raise ValueError("mapping not found")

            if claimed_at_value is None:
                connection.execute(
                    """
                    UPDATE access_mappings
                    SET status = ?,
                        claimed_at = '',
                        claimed_by = '',
                        target_site = CASE WHEN ? IS NULL THEN target_site ELSE ? END
                    WHERE id = ?
                    """,
                    (
                        normalized_status,
                        normalized_target_site,
                        normalized_target_site,
                        mapping_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE access_mappings
                    SET status = ?,
                        claimed_at = ?,
                        claimed_by = ?,
                        used_at = '',
                        target_site = CASE WHEN ? IS NULL THEN target_site ELSE ? END
                    WHERE id = ?
                    """,
                    (
                        normalized_status,
                        claimed_at_value,
                        normalized_claimed_by,
                        normalized_target_site,
                        normalized_target_site,
                        mapping_id,
                    ),
                )
            connection.commit()

        updated = self.get_by_id(mapping_id)
        if updated is None:
            raise ValueError("mapping not found")
        return updated

    def complete_workbench_mapping(
        self,
        mapping_id: int,
        category: str,
        target_site: str | None = None,
        claimed_by: str = "",
    ) -> AccessMapping:
        normalized_category = category.strip()
        normalized_target_site = None if target_site is None else target_site.strip()
        normalized_claimed_by = claimed_by.strip()
        completed_at = self._now()

        if not normalized_category:
            raise ValueError("category is required")
        if not normalized_claimed_by:
            raise ValueError("claimed_by is required")

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE access_mappings
                SET status = 'idle',
                    category = ?,
                    claimed_at = '',
                    claimed_by = '',
                    used_at = ?,
                    target_site = CASE WHEN ? IS NULL THEN target_site ELSE ? END
                WHERE id = ? AND status = 'in_progress' AND claimed_by = ?
                """,
                (
                    normalized_category,
                    completed_at,
                    normalized_target_site,
                    normalized_target_site,
                    mapping_id,
                    normalized_claimed_by,
                ),
            )
            connection.commit()

        if cursor.rowcount == 0:
            raise ValueError("mapping not claimed by this session")

        updated = self.get_by_id(mapping_id)
        if updated is None:
            raise ValueError("mapping not found")
        return updated

    def reset_mapping_status(self, mapping_id: int) -> AccessMapping:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE access_mappings
                SET status = 'idle',
                    claimed_at = '',
                    claimed_by = '',
                    last_seen_email_id = 0,
                    target_site = ''
                WHERE id = ?
                """,
                (mapping_id,),
            )
            connection.commit()

        if cursor.rowcount == 0:
            raise ValueError("mapping not found")

        updated = self.get_by_id(mapping_id)
        if updated is None:
            raise ValueError("mapping not found")
        return updated

    def save_cloudmail_settings(
        self,
        base_url: str,
        api_token: str,
        internal_admin_email: str = "",
        internal_admin_password: str = "",
        default_query_email: str = "",
        recent_email_limit: int | str = 10,
        display_timezone: str = "UTC",
    ) -> CloudMailSettingsRecord:
        normalized_base_url = base_url.strip()
        normalized_api_token = api_token.strip()
        normalized_internal_admin_email = internal_admin_email.strip().lower()
        normalized_internal_admin_password = internal_admin_password.strip()
        normalized_default_query_email = default_query_email.strip().lower()
        normalized_display_timezone = self._normalize_display_timezone(display_timezone)

        if not normalized_base_url:
            raise ValueError("base_url is required")
        if bool(normalized_internal_admin_email) != bool(normalized_internal_admin_password):
            raise ValueError("internal_admin_credentials incomplete")
        if not normalized_api_token and not (normalized_internal_admin_email and normalized_internal_admin_password):
            raise ValueError("cloudmail_auth is required")

        normalized_recent_email_limit = self._normalize_recent_email_limit(recent_email_limit)
        updated_at = self._now()

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
                    ("cloudmail_internal_admin_email", normalized_internal_admin_email, updated_at),
                    ("cloudmail_internal_admin_password", normalized_internal_admin_password, updated_at),
                    ("default_query_email", normalized_default_query_email, updated_at),
                    ("recent_email_limit", str(normalized_recent_email_limit), updated_at),
                    ("display_timezone", normalized_display_timezone, updated_at),
                ],
            )
            connection.commit()

        return CloudMailSettingsRecord(
            base_url=normalized_base_url,
            api_token=normalized_api_token,
            internal_admin_email=normalized_internal_admin_email,
            internal_admin_password=normalized_internal_admin_password,
            default_query_email=normalized_default_query_email,
            recent_email_limit=normalized_recent_email_limit,
            display_timezone=normalized_display_timezone,
            updated_at=updated_at,
        )

    def get_cloudmail_settings(
        self,
        default_base_url: str = "",
        default_api_token: str = "",
        default_internal_admin_email: str = "",
        default_internal_admin_password: str = "",
        default_query_email: str = "",
        default_recent_email_limit: int = 10,
        default_display_timezone: str = "UTC",
    ) -> CloudMailSettingsRecord:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT key, value, updated_at FROM app_settings WHERE key IN ('cloudmail_base_url', 'cloudmail_api_token', 'cloudmail_internal_admin_email', 'cloudmail_internal_admin_password', 'default_query_email', 'recent_email_limit', 'display_timezone')"
            ).fetchall()

        values = {
            "cloudmail_base_url": default_base_url,
            "cloudmail_api_token": default_api_token,
            "cloudmail_internal_admin_email": default_internal_admin_email,
            "cloudmail_internal_admin_password": default_internal_admin_password,
            "default_query_email": default_query_email,
            "recent_email_limit": str(default_recent_email_limit),
            "display_timezone": default_display_timezone,
        }
        updated_at = ""

        for row in rows:
            values[row["key"]] = row["value"]
            updated_at = max(updated_at, row["updated_at"])

        return CloudMailSettingsRecord(
            base_url=values["cloudmail_base_url"],
            api_token=values["cloudmail_api_token"],
            internal_admin_email=values["cloudmail_internal_admin_email"],
            internal_admin_password=values["cloudmail_internal_admin_password"],
            default_query_email=values["default_query_email"],
            recent_email_limit=self._normalize_recent_email_limit(values["recent_email_limit"]),
            display_timezone=self._normalize_display_timezone(values["display_timezone"]),
            updated_at=updated_at,
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS access_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient_email TEXT NOT NULL,
                    query_email TEXT NOT NULL DEFAULT '',
                    access_key TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'idle',
                    claimed_at TEXT NOT NULL DEFAULT '',
                    claimed_by TEXT NOT NULL DEFAULT '',
                    used_at TEXT NOT NULL DEFAULT '',
                    last_seen_email_id INTEGER NOT NULL DEFAULT 0,
                    target_site TEXT NOT NULL DEFAULT ''
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
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(access_mappings)").fetchall()
            }
            if "query_email" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN query_email TEXT NOT NULL DEFAULT ''"
                )
            if "category" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN category TEXT NOT NULL DEFAULT ''"
                )
            if "status" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN status TEXT NOT NULL DEFAULT 'idle'"
                )
            if "claimed_at" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN claimed_at TEXT NOT NULL DEFAULT ''"
                )
            if "claimed_by" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN claimed_by TEXT NOT NULL DEFAULT ''"
                )
            if "used_at" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN used_at TEXT NOT NULL DEFAULT ''"
                )
            if "last_seen_email_id" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN last_seen_email_id INTEGER NOT NULL DEFAULT 0"
                )
            if "target_site" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN target_site TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "UPDATE access_mappings SET query_email = recipient_email WHERE query_email = '' OR query_email IS NULL"
            )
            connection.execute(
                """
                UPDATE access_mappings
                SET status = CASE WHEN status = 'in_progress' THEN 'in_progress' ELSE 'idle' END,
                    claimed_at = CASE WHEN status = 'in_progress' THEN claimed_at ELSE '' END,
                    claimed_by = CASE WHEN status = 'in_progress' THEN claimed_by ELSE '' END
                WHERE status != 'idle' OR status IS NULL OR TRIM(status) = ''
                """
            )
            connection.execute(
                """
                UPDATE access_mappings
                SET status = 'idle',
                    claimed_at = '',
                    claimed_by = '',
                    target_site = ''
                WHERE status = 'in_progress' AND (claimed_by IS NULL OR TRIM(claimed_by) = '')
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _build_mapping_filters(search_query: str, category_filter: str) -> tuple[str, list[str]]:
        clauses: list[str] = []
        params: list[str] = []

        normalized_query = search_query.strip().lower()
        if normalized_query:
            wildcard = f"%{normalized_query}%"
            search_clause = " OR ".join(
                f"LOWER({column}) LIKE ?"
                for column in ("recipient_email", "query_email", "access_key", "label", "category")
            )
            clauses.append(f"({search_clause})")
            params.extend([wildcard] * 5)

        normalized_category = category_filter.strip().lower()
        if normalized_category:
            clauses.append("LOWER(category) = ?")
            params.append(normalized_category)

        return " AND ".join(clauses), params

    @staticmethod
    def _normalize_status(value: str) -> str:
        normalized = (value or "idle").strip().lower()
        normalized = LEGACY_MAPPING_STATUS_ALIASES.get(normalized, normalized)
        if normalized not in VALID_MAPPING_STATUSES:
            raise ValueError("status is invalid")
        return normalized

    @staticmethod
    def _normalize_recent_email_limit(value: int | str) -> int:
        try:
            normalized = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("recent_email_limit must be a positive integer") from exc
        if normalized <= 0:
            raise ValueError("recent_email_limit must be a positive integer")
        return normalized

    @staticmethod
    def _normalize_display_timezone(value: str) -> str:
        normalized = (value or "UTC").strip() or "UTC"
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("display_timezone is invalid") from exc
        return normalized

    @staticmethod
    def _generate_key() -> str:
        return secrets.token_urlsafe(12)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _row_to_mapping(row: sqlite3.Row | None) -> AccessMapping | None:
        if row is None:
            return None
        return AccessMapping(
            id=row["id"],
            recipient_email=row["recipient_email"],
            query_email=row["query_email"],
            access_key=row["access_key"],
            label=row["label"],
            category=row["category"],
            created_at=row["created_at"],
            status=row["status"] if row["status"] in VALID_MAPPING_STATUSES else "idle",
            claimed_at=row["claimed_at"],
            claimed_by=row["claimed_by"],
            used_at=row["used_at"],
            last_seen_email_id=int(row["last_seen_email_id"]),
            target_site=row["target_site"],
        )
