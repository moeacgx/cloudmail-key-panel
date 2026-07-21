from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.verification_extractor import (
    VALID_EXTRACTION_MODES,
    validate_custom_patterns,
    validate_openai_base_url,
)

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

VALID_ADDRESS_KINDS = {"primary", "icloud_alias"}
VALID_REUSE_POLICIES = {"reusable", "independent", "retired"}
VALID_DELIVERY_MODES = {"custom", "independent"}
VALID_ADDRESS_MODES = {"primary", "icloud_alias"}
VALID_BATCH_ADDRESS_MODES = VALID_ADDRESS_MODES | {"choice"}
VALID_CARD_STATUSES = {"active", "disabled"}
VALID_CLAIM_STATUSES = {"pending", "completed", "skipped", "timed_out"}
VALID_VERIFICATION_EVENT_SOURCES = {"public_card", "admin_workbench", "external_api"}
VALID_GLOBAL_EXTRACTION_MODES = {"off", "fallback", "only"}
_ICLOUD_ALIAS_TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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
    address_kind: str = "primary"
    parent_mapping_id: int = 0
    alias_tag: str = ""
    reuse_policy: str = "reusable"
    first_used_at: str = ""
    tags: tuple[str, ...] = ()
    claim_source_tag_id: int = 0


@dataclass(slots=True, frozen=True)
class CategoryOption:
    id: int
    name: str
    count: int


@dataclass(slots=True, frozen=True)
class TagOption:
    id: int
    name: str
    count: int
    color: str
    archived: bool
    success_count: int = 0
    kind: str = "service"
    sender_patterns: tuple[str, ...] = ()
    subject_keywords: tuple[str, ...] = ()
    prevents_reuse: bool = False
    alias_use_limit: int = 0
    code_patterns: tuple[str, ...] = ()
    extraction_mode: str = "rules"


@dataclass(slots=True, frozen=True)
class VerificationExtractionSettingsRecord:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int
    updated_at: str
    mode: str = "off"
    custom_patterns: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class CardCategory:
    id: int
    name: str
    card_count: int


@dataclass(slots=True, frozen=True)
class CardBatch:
    id: int
    name: str
    category_id: int
    category_name: str
    target_tag_id: int
    target_tag_name: str
    delivery_mode: str
    address_mode: str
    uses_per_card: int
    card_count: int
    expires_at: str
    created_at: str
    source_scope: str = "all_reusable"
    include_tag_ids: tuple[int, ...] = ()
    exclude_tag_ids: tuple[int, ...] = ()


@dataclass(slots=True, frozen=True)
class RedemptionCard:
    id: int
    batch_id: int
    code: str
    total_uses: int
    remaining_uses: int
    status: str
    consecutive_skips: int
    cooldown_until: str
    expires_at: str
    created_at: str


@dataclass(slots=True, frozen=True)
class RegistrationClaim:
    id: int
    card_id: int
    mapping_id: int
    root_mapping_id: int
    target_tag_id: int
    status: str
    address_mode: str
    recipient_email: str
    access_key: str
    verification_code: str
    email_id: int
    created_at: str
    completed_at: str
    ended_at: str
    root_email: str = ""
    query_email: str = ""
    target_tag_name: str = ""
    view_token: str = ""
    baseline_email_id: int = 0
    baseline_ready: bool = True
    revoked_at: str = ""
    superseded_at: str = ""


@dataclass(slots=True, frozen=True)
class VerificationEvent:
    id: int
    event_key: str
    root_mapping_id: int
    mapping_id: int
    tag_id: int
    source: str
    claim_id: int
    email_id: int
    recipient_email: str
    address_mode: str
    occurred_at: str


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
        normalized_category = self.canonicalize_category(category)
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
                if normalized_category:
                    category_row = connection.execute(
                        "SELECT id FROM categories WHERE normalized_name = ?",
                        (self._category_key(normalized_category),),
                    ).fetchone()
                    if category_row is not None:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO mapping_tags (mapping_id, tag_id, source, created_at)
                            VALUES (?, ?, 'legacy_category', ?)
                            """,
                            (int(cursor.lastrowid), int(category_row["id"]), created_at),
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
            tags=(normalized_category,) if normalized_category else (),
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
        normalized_category = self.canonicalize_category(category)

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
                if cursor.rowcount and normalized_category:
                    category_row = connection.execute(
                        "SELECT id FROM categories WHERE normalized_name = ?",
                        (self._category_key(normalized_category),),
                    ).fetchone()
                    if category_row is not None:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO mapping_tags (mapping_id, tag_id, source, created_at)
                            VALUES (?, ?, 'manual', ?)
                            """,
                            (mapping_id, int(category_row["id"]), self._now()),
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
        deleted_count = self.delete_mappings([mapping_id])
        if deleted_count == 0:
            raise ValueError("mapping not found")

    def get_by_id(self, mapping_id: int) -> AccessMapping | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, recipient_email, query_email, access_key, label, category, created_at, status, claimed_at,
                       claimed_by,
                       used_at, last_seen_email_id, target_site, address_kind, parent_mapping_id,
                       alias_tag, reuse_policy, first_used_at
                FROM access_mappings
                WHERE id = ?
                """,
                (mapping_id,),
            ).fetchone()

        return self._attach_mapping_tags(self._row_to_mapping(row))

    def get_by_key(self, access_key: str) -> AccessMapping | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, recipient_email, query_email, access_key, label, category, created_at, status, claimed_at,
                       claimed_by,
                       used_at, last_seen_email_id, target_site, address_kind, parent_mapping_id,
                       alias_tag, reuse_policy, first_used_at
                FROM access_mappings
                WHERE access_key = ?
                """,
                (access_key.strip(),),
            ).fetchone()

        return self._attach_mapping_tags(self._row_to_mapping(row))

    def list_mappings(
        self,
        search_query: str = "",
        category_filter: str = "",
        limit: int | None = None,
        offset: int = 0,
        *,
        include_aliases: bool = True,
    ) -> list[AccessMapping]:
        where_clause, params = self._build_mapping_filters(search_query, category_filter)
        if not include_aliases:
            where_clause = (
                f"({where_clause}) AND address_kind = 'primary'"
                if where_clause
                else "address_kind = 'primary'"
            )
        query = """
            SELECT id, recipient_email, query_email, access_key, label, category, created_at, status, claimed_at,
                   claimed_by,
                   used_at, last_seen_email_id, target_site, address_kind, parent_mapping_id,
                   alias_tag, reuse_policy, first_used_at
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

        return [
            attached
            for row in rows
            if (mapping := self._row_to_mapping(row)) is not None
            if (attached := self._attach_mapping_tags(mapping)) is not None
        ]

    def count_mappings(
        self,
        search_query: str = "",
        category_filter: str = "",
        *,
        include_aliases: bool = True,
    ) -> int:
        where_clause, params = self._build_mapping_filters(search_query, category_filter)
        if not include_aliases:
            where_clause = (
                f"({where_clause}) AND address_kind = 'primary'"
                if where_clause
                else "address_kind = 'primary'"
            )
        query = "SELECT COUNT(*) FROM access_mappings"
        if where_clause:
            query = f"{query} WHERE {where_clause}"

        with self._connect() as connection:
            return int(connection.execute(query, tuple(params)).fetchone()[0])

    def list_category_options(self, include_empty: bool = True) -> list[CategoryOption]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT categories.id, categories.name,
                       COUNT(DISTINCT mapping_tags.mapping_id) AS usage_count
                FROM categories
                LEFT JOIN mapping_tags ON mapping_tags.tag_id = categories.id
                WHERE categories.kind != 'system'
                GROUP BY categories.id
                ORDER BY categories.normalized_name ASC, categories.id ASC
                """
            ).fetchall()

        options = [
            CategoryOption(
                id=int(row["id"]),
                name=str(row["name"]),
                count=int(row["usage_count"] or 0),
            )
            for row in rows
        ]
        if include_empty:
            return options
        return [category for category in options if category.count > 0]

    def list_categories(self) -> list[str]:
        return [category.name for category in self.list_category_options(include_empty=False)]

    def get_category_name(self, category_id: int) -> str | None:
        try:
            normalized_id = int(category_id)
        except (TypeError, ValueError):
            return None
        if normalized_id <= 0:
            return None

        with self._connect() as connection:
            row = connection.execute(
                "SELECT name FROM categories WHERE id = ?",
                (normalized_id,),
            ).fetchone()
        return None if row is None else str(row["name"])

    def get_category_id(self, name: str) -> int | None:
        category_key = self._category_key(name)
        if not category_key:
            return None

        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM categories WHERE normalized_name = ?",
                (category_key,),
            ).fetchone()
        return None if row is None else int(row["id"])

    # ``categories`` 是旧版命名。升级后它作为标签字典继续复用，以保证 ID
    # 稳定并兼容现有 API；以下接口提供真正的多标签语义。
    def create_tag(
        self,
        name: str,
        color: str = "",
        *,
        kind: str = "service",
        sender_patterns: str | list[str] | tuple[str, ...] = (),
        subject_keywords: str | list[str] | tuple[str, ...] = (),
        prevents_reuse: bool | None = None,
        alias_use_limit: int | None = None,
        code_patterns: str | list[str] | tuple[str, ...] | None = None,
        extraction_mode: str | None = None,
    ) -> TagOption:
        canonical_name = self.canonicalize_category(name)
        if not canonical_name:
            raise ValueError("tag name is required")
        normalized_color = color.strip()
        if len(normalized_color) > 32:
            raise ValueError("tag color is too long")
        normalized_kind = (kind or "service").strip().lower()
        if normalized_kind not in {"service", "business", "system"}:
            raise ValueError("tag kind is invalid")
        normalized_senders = self._normalize_rule_values(sender_patterns)
        normalized_subjects = self._normalize_rule_values(subject_keywords)
        normalized_alias_use_limit = self._normalize_alias_use_limit(alias_use_limit)
        normalized_code_patterns = None if code_patterns is None else json.dumps(
            validate_custom_patterns(self._split_rule_lines(code_patterns)), ensure_ascii=False
        )
        normalized_extraction_mode = (
            None if extraction_mode is None else self._normalize_extraction_mode(extraction_mode)
        )

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE categories
                SET color = ?, archived = 0, kind = ?, sender_patterns = ?, subject_keywords = ?,
                    prevents_reuse = CASE WHEN ? IS NULL THEN prevents_reuse ELSE ? END,
                    alias_use_limit = CASE WHEN ? IS NULL THEN alias_use_limit ELSE ? END,
                    code_patterns = CASE WHEN ? IS NULL THEN code_patterns ELSE ? END,
                    extraction_mode = CASE WHEN ? IS NULL THEN extraction_mode ELSE ? END
                WHERE normalized_name = ?
                """,
                (
                    normalized_color,
                    normalized_kind,
                    json.dumps(normalized_senders, ensure_ascii=False),
                    json.dumps(normalized_subjects, ensure_ascii=False),
                    None if prevents_reuse is None else (1 if prevents_reuse else 0),
                    None if prevents_reuse is None else (1 if prevents_reuse else 0),
                    normalized_alias_use_limit,
                    normalized_alias_use_limit,
                    normalized_code_patterns,
                    normalized_code_patterns,
                    normalized_extraction_mode,
                    normalized_extraction_mode,
                    self._category_key(canonical_name),
                ),
            )
            row = connection.execute(
                """
                SELECT categories.id, categories.name, categories.color, categories.archived,
                       categories.kind, categories.sender_patterns, categories.subject_keywords,
                       categories.prevents_reuse, categories.alias_use_limit,
                       categories.code_patterns, categories.extraction_mode,
                       (
                           SELECT COUNT(DISTINCT COALESCE(NULLIF(tagged.parent_mapping_id, 0), tagged.id))
                           FROM mapping_tags tag_links
                           JOIN access_mappings tagged ON tagged.id = tag_links.mapping_id
                           WHERE tag_links.tag_id = categories.id
                       ) AS usage_count,
                       (
                           SELECT COUNT(*)
                           FROM verification_events success_events
                           WHERE success_events.tag_id = categories.id
                       ) AS success_count
                FROM categories
                WHERE categories.normalized_name = ?
                """,
                (self._category_key(canonical_name),),
            ).fetchone()
            connection.commit()
        if row is None:
            raise ValueError("tag not found")
        return self._row_to_tag(row)

    def list_tag_options(self, include_archived: bool = False) -> list[TagOption]:
        where_clause = "" if include_archived else "WHERE categories.archived = 0"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT categories.id, categories.name, categories.color, categories.archived,
                       categories.kind, categories.sender_patterns, categories.subject_keywords,
                       categories.prevents_reuse, categories.alias_use_limit,
                       categories.code_patterns, categories.extraction_mode,
                       (
                           SELECT COUNT(DISTINCT COALESCE(NULLIF(tagged.parent_mapping_id, 0), tagged.id))
                           FROM mapping_tags tag_links
                           JOIN access_mappings tagged ON tagged.id = tag_links.mapping_id
                           WHERE tag_links.tag_id = categories.id
                       ) AS usage_count,
                       (
                           SELECT COUNT(*)
                           FROM verification_events success_events
                           WHERE success_events.tag_id = categories.id
                       ) AS success_count
                FROM categories
                {where_clause}
                ORDER BY categories.normalized_name ASC, categories.id ASC
                """
            ).fetchall()
        return [self._row_to_tag(row) for row in rows]

    def get_tag(self, tag_id: int) -> TagOption | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT categories.id, categories.name, categories.color, categories.archived,
                       categories.kind, categories.sender_patterns, categories.subject_keywords,
                       categories.prevents_reuse, categories.alias_use_limit,
                       categories.code_patterns, categories.extraction_mode,
                       (
                           SELECT COUNT(DISTINCT COALESCE(NULLIF(tagged.parent_mapping_id, 0), tagged.id))
                           FROM mapping_tags tag_links
                           JOIN access_mappings tagged ON tagged.id = tag_links.mapping_id
                           WHERE tag_links.tag_id = categories.id
                       ) AS usage_count,
                       (
                           SELECT COUNT(*)
                           FROM verification_events success_events
                           WHERE success_events.tag_id = categories.id
                       ) AS success_count
                FROM categories
                WHERE categories.id = ?
                """,
                (int(tag_id),),
            ).fetchone()
        return None if row is None else self._row_to_tag(row)

    def list_tags(self, include_archived: bool = False) -> list[str]:
        return [tag.name for tag in self.list_tag_options(include_archived=include_archived)]

    def ensure_independent_system_tag(self) -> TagOption:
        """返回独立邮箱批次使用的内部占位标签，不把它写入邮箱标签历史。"""
        system_name = "独立邮箱（系统）"
        existing_id = self.get_category_id(system_name)
        if existing_id is not None:
            existing = self.get_tag(existing_id)
            if existing is not None and existing.kind == "system":
                return existing
        return self.create_tag(system_name, "#64748b", kind="system")

    def rename_tag(
        self,
        tag_id: int,
        name: str,
        color: str | None = None,
        *,
        kind: str | None = None,
        sender_patterns: str | list[str] | tuple[str, ...] | None = None,
        subject_keywords: str | list[str] | tuple[str, ...] | None = None,
        prevents_reuse: bool | None = None,
        alias_use_limit: int | None = None,
        code_patterns: str | list[str] | tuple[str, ...] | None = None,
        extraction_mode: str | None = None,
    ) -> TagOption:
        normalized_name = unicodedata.normalize("NFKC", name or "").strip()
        normalized_key = self._category_key(normalized_name)
        if not normalized_key:
            raise ValueError("tag name is required")
        normalized_color = None if color is None else color.strip()
        if normalized_color is not None and len(normalized_color) > 32:
            raise ValueError("tag color is too long")
        normalized_kind = None if kind is None else kind.strip().lower()
        if normalized_kind is not None and normalized_kind not in {"service", "business", "system"}:
            raise ValueError("tag kind is invalid")
        normalized_senders = None if sender_patterns is None else json.dumps(
            self._normalize_rule_values(sender_patterns), ensure_ascii=False
        )
        normalized_subjects = None if subject_keywords is None else json.dumps(
            self._normalize_rule_values(subject_keywords), ensure_ascii=False
        )
        normalized_alias_use_limit = self._normalize_alias_use_limit(alias_use_limit)
        normalized_code_patterns = None if code_patterns is None else json.dumps(
            validate_custom_patterns(self._split_rule_lines(code_patterns)), ensure_ascii=False
        )
        normalized_extraction_mode = (
            None if extraction_mode is None else self._normalize_extraction_mode(extraction_mode)
        )

        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT name FROM categories WHERE id = ?",
                    (int(tag_id),),
                ).fetchone()
                if existing is None:
                    raise ValueError("tag not found")
                old_name = str(existing["name"])
                cursor = connection.execute(
                    """
                    UPDATE categories
                    SET name = ?, normalized_name = ?,
                        color = CASE WHEN ? IS NULL THEN color ELSE ? END,
                        kind = CASE WHEN ? IS NULL THEN kind ELSE ? END,
                        sender_patterns = CASE WHEN ? IS NULL THEN sender_patterns ELSE ? END,
                        subject_keywords = CASE WHEN ? IS NULL THEN subject_keywords ELSE ? END,
                        prevents_reuse = CASE WHEN ? IS NULL THEN prevents_reuse ELSE ? END,
                        alias_use_limit = CASE WHEN ? IS NULL THEN alias_use_limit ELSE ? END,
                        code_patterns = CASE WHEN ? IS NULL THEN code_patterns ELSE ? END,
                        extraction_mode = CASE WHEN ? IS NULL THEN extraction_mode ELSE ? END
                    WHERE id = ?
                    """,
                    (
                        normalized_name,
                        normalized_key,
                        normalized_color,
                        normalized_color,
                        normalized_kind,
                        normalized_kind,
                        normalized_senders,
                        normalized_senders,
                        normalized_subjects,
                        normalized_subjects,
                        None if prevents_reuse is None else (1 if prevents_reuse else 0),
                        None if prevents_reuse is None else (1 if prevents_reuse else 0),
                        normalized_alias_use_limit,
                        normalized_alias_use_limit,
                        normalized_code_patterns,
                        normalized_code_patterns,
                        normalized_extraction_mode,
                        normalized_extraction_mode,
                        int(tag_id),
                    ),
                )
                connection.execute(
                    "UPDATE access_mappings SET category = ? WHERE category = ?",
                    (normalized_name, old_name),
                )
                connection.execute(
                    "UPDATE access_mappings SET target_site = ? WHERE target_site = ?",
                    (normalized_name, old_name),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("tag already exists") from exc
        if cursor.rowcount == 0:
            raise ValueError("tag not found")
        return next(tag for tag in self.list_tag_options(include_archived=True) if tag.id == int(tag_id))

    def set_tag_prevents_reuse(self, tag_id: int, prevents_reuse: bool = True) -> TagOption:
        """设置标签的全局独立账号策略。

        领取邮箱时会动态检查主邮箱的所有标签。只要其中任意标签开启该
        策略，该邮箱便不会进入任何可复用领取池；无需破坏邮箱自身的
        ``reuse_policy``，关闭标签策略后也能恢复原有资格。
        """

        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE categories SET prevents_reuse = ? WHERE id = ?",
                (1 if prevents_reuse else 0, int(tag_id)),
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise ValueError("tag not found")
        tag = self.get_tag(int(tag_id))
        if tag is None:
            raise ValueError("tag not found")
        return tag

    def set_tag_alias_use_limit(self, tag_id: int, alias_use_limit: int = 0) -> TagOption:
        """设置单个主邮箱在该平台标签下允许生成的裂变地址数量。

        ``0`` 表示不限制。计数以内部保留的裂变地址历史为准，跳过、超时和
        成功领取均会计入，避免反复生成地址绕过平台自身的别名数量限制。
        """

        normalized_limit = self._normalize_alias_use_limit(alias_use_limit)
        assert normalized_limit is not None
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE categories SET alias_use_limit = ? WHERE id = ?",
                (normalized_limit, int(tag_id)),
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise ValueError("tag not found")
        tag = self.get_tag(int(tag_id))
        if tag is None:
            raise ValueError("tag not found")
        return tag

    def set_tag_archived(self, tag_id: int, archived: bool = True) -> TagOption:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE categories SET archived = ? WHERE id = ?",
                (1 if archived else 0, int(tag_id)),
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise ValueError("tag not found")
        return next(tag for tag in self.list_tag_options(include_archived=True) if tag.id == int(tag_id))

    def delete_tag(self, tag_id: int) -> None:
        """彻底删除没有被任何业务数据引用的普通标签。

        已进入邮箱标签、兑换卡配置或接码流水的标签属于历史数据，必须通过
        归档停用，不能硬删除。这样既提供真正的删除能力，也不会造成历史统计
        和兑换卡配置失真。
        """

        normalized_id = int(tag_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tag = connection.execute(
                "SELECT name, kind FROM categories WHERE id = ?",
                (normalized_id,),
            ).fetchone()
            if tag is None:
                raise ValueError("tag not found")
            if str(tag["kind"]) == "system":
                raise ValueError("system tag cannot be deleted")

            direct_references = (
                ("mapping_tags", "tag_id"),
                ("alias_generation_events", "tag_id"),
                ("card_batches", "target_tag_id"),
                ("registration_claims", "target_tag_id"),
                ("verification_events", "tag_id"),
            )
            for table_name, column_name in direct_references:
                referenced = connection.execute(
                    f"SELECT 1 FROM {table_name} WHERE {column_name} = ? LIMIT 1",
                    (normalized_id,),
                ).fetchone()
                if referenced is not None:
                    raise ValueError("tag is in use")

            # 批次的包含/排除标签以 JSON 数组保存，没有外键约束，也必须检查。
            for row in connection.execute(
                "SELECT include_tag_ids, exclude_tag_ids FROM card_batches"
            ).fetchall():
                for raw_ids in (row["include_tag_ids"], row["exclude_tag_ids"]):
                    try:
                        configured_ids = {int(value) for value in json.loads(str(raw_ids or "[]"))}
                    except (TypeError, ValueError, json.JSONDecodeError):
                        configured_ids = set()
                    if normalized_id in configured_ids:
                        raise ValueError("tag is in use")

            # 兼容旧数据：早期版本只在字符串字段里保存分类/目标平台。
            tag_name = str(tag["name"])
            legacy_reference = connection.execute(
                """
                SELECT 1
                FROM access_mappings
                WHERE category = ? OR target_site = ?
                LIMIT 1
                """,
                (tag_name, tag_name),
            ).fetchone()
            if legacy_reference is not None:
                raise ValueError("tag is in use")

            cursor = connection.execute(
                "DELETE FROM categories WHERE id = ?",
                (normalized_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError("tag not found")
            connection.commit()

    def list_mapping_tags(self, mapping_id: int) -> list[TagOption]:
        root_id = self.get_root_mapping_id(mapping_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT categories.id, categories.name, categories.color, categories.archived,
                       categories.kind, categories.sender_patterns, categories.subject_keywords,
                       categories.prevents_reuse, categories.alias_use_limit,
                       categories.code_patterns, categories.extraction_mode,
                       1 AS usage_count,
                       (
                           SELECT COUNT(*)
                           FROM verification_events success_events
                           WHERE success_events.tag_id = categories.id
                       ) AS success_count
                FROM mapping_tags
                JOIN categories ON categories.id = mapping_tags.tag_id
                WHERE mapping_tags.mapping_id = ?
                ORDER BY categories.normalized_name ASC, categories.id ASC
                """,
                (root_id,),
            ).fetchall()
        return [self._row_to_tag(row) for row in rows]

    def add_mapping_tag(self, mapping_id: int, tag_id: int, source: str = "manual") -> None:
        normalized_source = source.strip() or "manual"
        root_id = self.get_root_mapping_id(mapping_id)
        with self._connect() as connection:
            tag = connection.execute(
                "SELECT id FROM categories WHERE id = ?",
                (int(tag_id),),
            ).fetchone()
            if tag is None:
                raise ValueError("tag not found")
            connection.execute(
                """
                INSERT INTO mapping_tags (mapping_id, tag_id, source, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(mapping_id, tag_id) DO UPDATE SET
                    source = CASE
                        WHEN mapping_tags.source = 'usage' THEN mapping_tags.source
                        ELSE excluded.source
                    END
                """,
                (root_id, int(tag_id), normalized_source, self._now()),
            )
            connection.commit()

    def remove_mapping_tag(self, mapping_id: int, tag_id: int) -> None:
        root_id = self.get_root_mapping_id(mapping_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source FROM mapping_tags WHERE mapping_id = ? AND tag_id = ?",
                (root_id, int(tag_id)),
            ).fetchone()
            if row is None:
                raise ValueError("mapping tag not found")
            if str(row["source"]) == "usage":
                raise ValueError("usage tag cannot be removed")
            connection.execute(
                "DELETE FROM mapping_tags WHERE mapping_id = ? AND tag_id = ?",
                (root_id, int(tag_id)),
            )
            remaining_rows = connection.execute(
                """
                SELECT tag_id
                FROM mapping_tags
                WHERE mapping_id = ? AND source != 'usage'
                ORDER BY tag_id ASC
                """,
                (root_id,),
            ).fetchall()
            self._sync_legacy_mapping_category(
                connection,
                root_id,
                [int(item["tag_id"]) for item in remaining_rows],
            )
            connection.commit()

    def set_mapping_tags(self, mapping_id: int, tag_ids: list[int] | tuple[int, ...]) -> None:
        """同步人工标签，同时保留由成功接码固化的使用标签。"""
        root_id = self.get_root_mapping_id(mapping_id)
        normalized_ids = self._normalize_tag_ids(tag_ids)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if normalized_ids:
                placeholders = ", ".join("?" for _ in normalized_ids)
                rows = connection.execute(
                    f"SELECT id FROM categories WHERE id IN ({placeholders}) AND archived = 0",
                    normalized_ids,
                ).fetchall()
                if len(rows) != len(normalized_ids):
                    connection.rollback()
                    raise ValueError("tag not found")
            existing_rows = connection.execute(
                "SELECT tag_id, source FROM mapping_tags WHERE mapping_id = ?",
                (root_id,),
            ).fetchall()
            existing = {int(row["tag_id"]): str(row["source"]) for row in existing_rows}
            removable = [
                tag_id
                for tag_id, source in existing.items()
                if source != "usage" and tag_id not in normalized_ids
            ]
            if removable:
                placeholders = ", ".join("?" for _ in removable)
                connection.execute(
                    f"DELETE FROM mapping_tags WHERE mapping_id = ? AND tag_id IN ({placeholders})",
                    (root_id, *removable),
                )
            now = self._now()
            for tag_id in normalized_ids:
                connection.execute(
                    """
                    INSERT INTO mapping_tags (mapping_id, tag_id, source, created_at)
                    VALUES (?, ?, 'manual', ?)
                    ON CONFLICT(mapping_id, tag_id) DO UPDATE SET source = CASE
                        WHEN mapping_tags.source = 'usage' THEN 'usage'
                        ELSE 'manual'
                    END
                    """,
                    (root_id, tag_id, now),
                )
            # 旧版单分类字段仍会在启动迁移时作为标签来源，因此人工改标签时必须
            # 同步更新它，否则服务重启会把已经移除的旧标签重新回填。
            self._sync_legacy_mapping_category(connection, root_id, normalized_ids)
            connection.commit()

    @staticmethod
    def _sync_legacy_mapping_category(
        connection: sqlite3.Connection,
        mapping_id: int,
        tag_ids: list[int] | tuple[int, ...],
    ) -> None:
        normalized_ids = tuple(dict.fromkeys(int(tag_id) for tag_id in tag_ids))
        legacy_category = ""
        if normalized_ids:
            placeholders = ", ".join("?" for _ in normalized_ids)
            rows = connection.execute(
                f"SELECT id, name, kind FROM categories WHERE id IN ({placeholders})",
                normalized_ids,
            ).fetchall()
            by_id = {int(row["id"]): row for row in rows}
            ordered = [by_id[tag_id] for tag_id in normalized_ids if tag_id in by_id]
            preferred = next(
                (row for row in ordered if str(row["kind"]) == "business"),
                ordered[0] if ordered else None,
            )
            if preferred is not None:
                legacy_category = str(preferred["name"])
        connection.execute(
            "UPDATE access_mappings SET category = ? WHERE id = ?",
            (legacy_category, int(mapping_id)),
        )

    def list_verification_events(
        self,
        *,
        tag_id: int | None = None,
        root_mapping_id: int | None = None,
        limit: int = 100,
    ) -> list[VerificationEvent]:
        """列出可审计的成功接码流水，不从邮箱标签反推历史次数。"""

        clauses: list[str] = []
        params: list[object] = []
        if tag_id is not None:
            clauses.append("tag_id = ?")
            params.append(int(tag_id))
        if root_mapping_id is not None:
            clauses.append("root_mapping_id = ?")
            params.append(int(root_mapping_id))
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM verification_events
                {where_clause}
                ORDER BY occurred_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_verification_event(row) for row in rows]

    def count_verification_events(
        self,
        *,
        tag_id: int | None = None,
        root_mapping_id: int | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[object] = []
        if tag_id is not None:
            clauses.append("tag_id = ?")
            params.append(int(tag_id))
        if root_mapping_id is not None:
            clauses.append("root_mapping_id = ?")
            params.append(int(root_mapping_id))
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS event_count FROM verification_events {where_clause}",
                tuple(params),
            ).fetchone()
        return int(row["event_count"] if row is not None else 0)

    def get_root_mapping_id(self, mapping_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, parent_mapping_id FROM access_mappings WHERE id = ?",
                (int(mapping_id),),
            ).fetchone()
        if row is None:
            raise ValueError("mapping not found")
        parent_mapping_id = int(row["parent_mapping_id"] or 0)
        return parent_mapping_id or int(row["id"])

    def list_icloud_aliases(self, parent_mapping_id: int) -> list[AccessMapping]:
        root_id = self.get_root_mapping_id(parent_mapping_id)
        if root_id != int(parent_mapping_id):
            raise ValueError("parent mapping is required")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM access_mappings WHERE parent_mapping_id = ? ORDER BY id DESC",
                (root_id,),
            ).fetchall()
        return [
            attached
            for row in rows
            if (mapping := self._row_to_mapping(row)) is not None
            if (attached := self._attach_mapping_tags(mapping)) is not None
        ]

    def create_icloud_alias(
        self,
        parent_mapping_id: int,
        alias_tag: str | None = None,
        access_key: str | None = None,
        target_site: str = "",
    ) -> AccessMapping:
        normalized_key = (access_key or self._generate_key()).strip()
        normalized_target_site = target_site.strip()
        if not normalized_key:
            raise ValueError("access_key is required")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent = connection.execute(
                "SELECT * FROM access_mappings WHERE id = ?",
                (int(parent_mapping_id),),
            ).fetchone()
            if parent is None:
                connection.rollback()
                raise ValueError("mapping not found")
            if str(parent["address_kind"] or "primary") != "primary" or int(parent["parent_mapping_id"] or 0):
                connection.rollback()
                raise ValueError("icloud alias requires a primary mapping")
            if str(parent["reuse_policy"] or "reusable") != "reusable":
                connection.rollback()
                raise ValueError("mapping is not reusable")

            target_tag = connection.execute(
                "SELECT id, alias_use_limit FROM categories WHERE normalized_name = ?",
                (self._category_key(normalized_target_site),),
            ).fetchone()
            if target_tag is not None and int(target_tag["alias_use_limit"] or 0) > 0:
                generated_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM alias_generation_events
                        WHERE root_mapping_id = ? AND tag_id = ?
                        """,
                        (int(parent_mapping_id), int(target_tag["id"])),
                    ).fetchone()[0]
                )
                if generated_count >= int(target_tag["alias_use_limit"]):
                    connection.rollback()
                    raise ValueError("alias use limit reached")

            parent_email = str(parent["recipient_email"]).strip().lower()
            if "@" not in parent_email:
                connection.rollback()
                raise ValueError("invalid parent email")
            local_part, domain = parent_email.rsplit("@", 1)
            if domain != "icloud.com" or "+" in local_part:
                connection.rollback()
                raise ValueError("icloud alias requires a non-alias icloud.com address")

            requested_tag = "" if alias_tag is None else alias_tag.strip().lower()
            if requested_tag and not _ICLOUD_ALIAS_TAG_PATTERN.fullmatch(requested_tag):
                connection.rollback()
                raise ValueError("invalid icloud alias tag")

            candidate_tag = requested_tag
            for _attempt in range(32):
                if not candidate_tag:
                    candidate_tag = secrets.token_hex(5)
                recipient_email = f"{local_part}+{candidate_tag}@{domain}"
                duplicate = connection.execute(
                    "SELECT 1 FROM access_mappings WHERE LOWER(recipient_email) = ? LIMIT 1",
                    (recipient_email,),
                ).fetchone()
                if duplicate is None:
                    break
                if requested_tag:
                    connection.rollback()
                    raise ValueError("icloud alias already exists")
                candidate_tag = ""
            else:
                connection.rollback()
                raise ValueError("unable to generate unique icloud alias")

            created_at = self._now()
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO access_mappings (
                        recipient_email, query_email, access_key, label, category, created_at,
                        status, claimed_at, claimed_by, used_at, last_seen_email_id, target_site,
                        address_kind, parent_mapping_id, alias_tag, reuse_policy, first_used_at
                    )
                    VALUES (?, ?, ?, '', ?, ?, 'idle', '', '', '', 0, ?,
                            'icloud_alias', ?, ?, 'reusable', '')
                    """,
                    (
                        recipient_email,
                        str(parent["query_email"]),
                        normalized_key,
                        str(parent["category"] or ""),
                        created_at,
                        normalized_target_site,
                        int(parent_mapping_id),
                        candidate_tag,
                    ),
                )
                alias_id = int(cursor.lastrowid)
                if target_tag is not None:
                    connection.execute(
                        """
                        INSERT INTO alias_generation_events (
                            mapping_id, root_mapping_id, tag_id, created_at
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            alias_id,
                            int(parent_mapping_id),
                            int(target_tag["id"]),
                            created_at,
                        ),
                    )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ValueError("access_key already exists") from exc

        alias = self.get_by_id(alias_id)
        if alias is None:
            raise ValueError("mapping not found")
        return alias

    def set_mapping_reuse_policy(self, mapping_id: int, reuse_policy: str) -> AccessMapping:
        normalized_policy = reuse_policy.strip().lower()
        if normalized_policy not in VALID_REUSE_POLICIES:
            raise ValueError("invalid reuse policy")
        root_id = self.get_root_mapping_id(mapping_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if normalized_policy != "reusable":
                pending = connection.execute(
                    """
                    SELECT 1 FROM registration_claims
                    WHERE root_mapping_id = ? AND status = 'pending'
                    LIMIT 1
                    """,
                    (root_id,),
                ).fetchone()
                if pending is not None:
                    connection.rollback()
                    raise ValueError("mapping has an active registration claim")
            connection.execute(
                "UPDATE access_mappings SET reuse_policy = ? WHERE id = ?",
                (normalized_policy, root_id),
            )
            connection.commit()
        mapping = self.get_by_id(root_id)
        if mapping is None:
            raise ValueError("mapping not found")
        return mapping

    def is_mapping_fully_unused(self, mapping_id: int) -> bool:
        root_id = self.get_root_mapping_id(mapping_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT first_used_at,
                       EXISTS(
                           SELECT 1 FROM registration_claims
                           WHERE root_mapping_id = access_mappings.id AND status = 'completed'
                       ) AS has_completed_claim
                FROM access_mappings
                WHERE id = ?
                """,
                (root_id,),
            ).fetchone()
        return row is not None and not str(row["first_used_at"] or "") and not bool(row["has_completed_claim"])

    def create_card_category(self, name: str) -> CardCategory:
        normalized_name = unicodedata.normalize("NFKC", name or "").strip()
        normalized_key = self._category_key(normalized_name)
        if not normalized_key:
            raise ValueError("card category name is required")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO card_categories (name, normalized_name, created_at)
                VALUES (?, ?, ?)
                """,
                (normalized_name, normalized_key, self._now()),
            )
            row = connection.execute(
                """
                SELECT card_categories.id, card_categories.name,
                       COUNT(redemption_cards.id) AS card_count
                FROM card_categories
                LEFT JOIN card_batches ON card_batches.category_id = card_categories.id
                LEFT JOIN redemption_cards ON redemption_cards.batch_id = card_batches.id
                WHERE card_categories.normalized_name = ?
                GROUP BY card_categories.id
                """,
                (normalized_key,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise ValueError("card category not found")
        return self._row_to_card_category(row)

    def list_card_categories(self) -> list[CardCategory]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT card_categories.id, card_categories.name,
                       COUNT(redemption_cards.id) AS card_count
                FROM card_categories
                LEFT JOIN card_batches ON card_batches.category_id = card_categories.id
                LEFT JOIN redemption_cards ON redemption_cards.batch_id = card_batches.id
                GROUP BY card_categories.id
                ORDER BY card_categories.normalized_name ASC, card_categories.id ASC
                """
            ).fetchall()
        return [self._row_to_card_category(row) for row in rows]

    def create_card_batch(
        self,
        *,
        name: str,
        category_id: int,
        target_tag_id: int,
        card_count: int,
        uses_per_card: int,
        delivery_mode: str = "custom",
        address_mode: str = "primary",
        source_scope: str = "all_reusable",
        include_tag_ids: list[int] | tuple[int, ...] | None = None,
        exclude_tag_ids: list[int] | tuple[int, ...] | None = None,
        expires_at: str = "",
        expiry_timezone: str = "UTC",
    ) -> tuple[CardBatch, list[RedemptionCard]]:
        normalized_name = name.strip()
        normalized_delivery_mode = delivery_mode.strip().lower()
        normalized_address_mode = address_mode.strip().lower()
        normalized_source_scope = source_scope.strip().lower() or "all_reusable"
        normalized_include_tags = self._normalize_tag_ids(include_tag_ids)
        normalized_exclude_tags = self._normalize_tag_ids(exclude_tag_ids)
        normalized_expires_at = self._normalize_expiry(expires_at, expiry_timezone)
        try:
            normalized_card_count = int(card_count)
            normalized_uses = int(uses_per_card)
        except (TypeError, ValueError) as exc:
            raise ValueError("card count and uses must be positive integers") from exc
        if not normalized_name:
            raise ValueError("batch name is required")
        if not 1 <= normalized_card_count <= 10_000:
            raise ValueError("card count must be between 1 and 10000")
        if not 1 <= normalized_uses <= 1_000_000:
            raise ValueError("uses per card must be between 1 and 1000000")
        if normalized_delivery_mode not in VALID_DELIVERY_MODES:
            raise ValueError("invalid delivery mode")
        if normalized_address_mode not in VALID_BATCH_ADDRESS_MODES:
            raise ValueError("invalid address mode")
        if normalized_source_scope not in {"never_used", "used_reusable", "all_reusable"}:
            raise ValueError("invalid source scope")
        if set(normalized_include_tags) & set(normalized_exclude_tags):
            raise ValueError("tag filters conflict")
        if normalized_delivery_mode == "independent" and normalized_address_mode != "primary":
            raise ValueError("independent delivery only supports primary addresses")
        if normalized_delivery_mode == "independent":
            normalized_source_scope = "never_used"

        created_at = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            category = connection.execute(
                "SELECT id FROM card_categories WHERE id = ?",
                (int(category_id),),
            ).fetchone()
            if category is None:
                connection.rollback()
                raise ValueError("card category not found")
            target_tag = connection.execute(
                "SELECT id FROM categories WHERE id = ? AND archived = 0",
                (int(target_tag_id),),
            ).fetchone()
            if target_tag is None:
                connection.rollback()
                raise ValueError("target tag not found")
            filter_tag_ids = (*normalized_include_tags, *normalized_exclude_tags)
            if filter_tag_ids:
                placeholders = ", ".join("?" for _ in filter_tag_ids)
                found_count = connection.execute(
                    f"SELECT COUNT(*) AS count FROM categories WHERE id IN ({placeholders})",
                    filter_tag_ids,
                ).fetchone()
                if found_count is None or int(found_count["count"]) != len(set(filter_tag_ids)):
                    connection.rollback()
                    raise ValueError("tag filter not found")
            cursor = connection.execute(
                """
                INSERT INTO card_batches (
                    name, category_id, target_tag_id, delivery_mode, address_mode,
                    uses_per_card, source_scope, include_tag_ids, exclude_tag_ids,
                    expires_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_name,
                    int(category_id),
                    int(target_tag_id),
                    normalized_delivery_mode,
                    normalized_address_mode,
                    normalized_uses,
                    normalized_source_scope,
                    json.dumps(normalized_include_tags),
                    json.dumps(normalized_exclude_tags),
                    normalized_expires_at,
                    created_at,
                ),
            )
            batch_id = int(cursor.lastrowid)
            codes: list[str] = []
            code_set: set[str] = set()
            while len(codes) < normalized_card_count:
                code = self._generate_card_code()
                if code not in code_set:
                    codes.append(code)
                    code_set.add(code)
            connection.executemany(
                """
                INSERT INTO redemption_cards (
                    batch_id, code, total_uses, remaining_uses, status,
                    consecutive_skips, cooldown_until, expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'active', 0, '', ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        code,
                        normalized_uses,
                        normalized_uses,
                        normalized_expires_at,
                        created_at,
                        created_at,
                    )
                    for code in codes
                ],
            )
            connection.commit()

        batch = self.get_card_batch(batch_id)
        if batch is None:
            raise ValueError("card batch not found")
        return batch, self.list_redemption_cards(batch_id=batch_id)

    def get_card_batch(self, batch_id: int) -> CardBatch | None:
        with self._connect() as connection:
            row = connection.execute(
                self._card_batch_select() + " WHERE card_batches.id = ? GROUP BY card_batches.id",
                (int(batch_id),),
            ).fetchone()
        return None if row is None else self._row_to_card_batch(row)

    def list_card_batches(self, category_id: int | None = None) -> list[CardBatch]:
        where_clause = ""
        params: tuple[object, ...] = ()
        if category_id is not None:
            where_clause = " WHERE card_batches.category_id = ?"
            params = (int(category_id),)
        with self._connect() as connection:
            rows = connection.execute(
                self._card_batch_select()
                + where_clause
                + " GROUP BY card_batches.id ORDER BY card_batches.id DESC",
                params,
            ).fetchall()
        return [self._row_to_card_batch(row) for row in rows]

    def list_redemption_cards(
        self,
        *,
        batch_id: int | None = None,
        category_id: int | None = None,
        search_query: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[RedemptionCard]:
        clauses: list[str] = []
        params: list[object] = []
        if batch_id is not None:
            clauses.append("redemption_cards.batch_id = ?")
            params.append(int(batch_id))
        if category_id is not None:
            clauses.append("card_batches.category_id = ?")
            params.append(int(category_id))
        normalized_query = search_query.strip().upper()
        if normalized_query:
            clauses.append("(redemption_cards.code LIKE ? OR UPPER(card_batches.name) LIKE ?)")
            wildcard = f"%{normalized_query}%"
            params.extend((wildcard, wildcard))
        query = """
            SELECT redemption_cards.*
            FROM redemption_cards
            JOIN card_batches ON card_batches.id = redemption_cards.batch_id
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY redemption_cards.id DESC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([max(int(limit), 1), max(int(offset), 0)])
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_redemption_card(row) for row in rows]

    def count_redemption_cards(
        self,
        *,
        category_id: int | None = None,
        search_query: str = "",
    ) -> int:
        clauses: list[str] = []
        params: list[object] = []
        if category_id is not None:
            clauses.append("card_batches.category_id = ?")
            params.append(int(category_id))
        normalized_query = search_query.strip().upper()
        if normalized_query:
            clauses.append("(redemption_cards.code LIKE ? OR UPPER(card_batches.name) LIKE ?)")
            wildcard = f"%{normalized_query}%"
            params.extend((wildcard, wildcard))
        query = """
            SELECT COUNT(*)
            FROM redemption_cards
            JOIN card_batches ON card_batches.id = redemption_cards.batch_id
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._connect() as connection:
            return int(connection.execute(query, tuple(params)).fetchone()[0])

    def get_redemption_card_summary(self, *, now: str) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE
                        WHEN status = 'active' AND remaining_uses > 0
                         AND (expires_at = '' OR expires_at > ?) THEN 1 ELSE 0 END), 0) AS active,
                    COALESCE(SUM(CASE
                        WHEN remaining_uses <= 0 OR (expires_at != '' AND expires_at <= ?)
                        THEN 1 ELSE 0 END), 0) AS exhausted,
                    COALESCE(SUM(total_uses - remaining_uses), 0) AS consumed_uses
                FROM redemption_cards
                """,
                (now, now),
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "active": int(row["active"] or 0),
            "exhausted": int(row["exhausted"] or 0),
            "consumed_uses": int(row["consumed_uses"] or 0),
        }

    def get_card_by_code(self, code: str) -> RedemptionCard | None:
        normalized_code = self._normalize_card_code(code)
        if not normalized_code:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM redemption_cards WHERE code = ?",
                (normalized_code,),
            ).fetchone()
        return None if row is None else self._row_to_redemption_card(row)

    def get_redemption_card(self, card_id: int) -> RedemptionCard | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM redemption_cards WHERE id = ?",
                (int(card_id),),
            ).fetchone()
        return None if row is None else self._row_to_redemption_card(row)

    def set_card_status(self, card_ids: list[int], status: str) -> int:
        normalized_status = status.strip().lower()
        if normalized_status not in VALID_CARD_STATUSES:
            raise ValueError("invalid card status")
        normalized_ids = sorted({int(card_id) for card_id in card_ids})
        if not normalized_ids:
            raise ValueError("card_ids is required")
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE redemption_cards SET status = ?, updated_at = ? WHERE id IN ({placeholders})",
                (normalized_status, self._now(), *normalized_ids),
            )
            connection.commit()
        return int(cursor.rowcount)

    def add_card_uses(self, card_ids: list[int], uses: int) -> int:
        normalized_ids = sorted({int(card_id) for card_id in card_ids})
        normalized_uses = int(uses)
        if not normalized_ids:
            raise ValueError("card_ids is required")
        if normalized_uses <= 0:
            raise ValueError("uses must be positive")
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE redemption_cards
                SET total_uses = total_uses + ?, remaining_uses = remaining_uses + ?, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (normalized_uses, normalized_uses, self._now(), *normalized_ids),
            )
            connection.commit()
        return int(cursor.rowcount)

    def start_registration_claim(
        self,
        card_code: str,
        *,
        address_mode: str | None = None,
        alias_tag: str | None = None,
        baseline_email_id: int = 0,
        timeout_minutes: int = 30,
        defer_email_baseline: bool = False,
    ) -> RegistrationClaim:
        normalized_code = self._normalize_card_code(card_code)
        if not normalized_code:
            raise ValueError("card code is required")
        requested_mode = None if address_mode is None else address_mode.strip().lower()
        if requested_mode is not None and requested_mode not in VALID_ADDRESS_MODES:
            raise ValueError("invalid address mode")
        now = self._now()
        normalized_timeout = max(1, int(timeout_minutes))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_pending_claims(
                connection,
                now=now,
                timeout_minutes=normalized_timeout,
            )
            card = connection.execute(
                """
                SELECT redemption_cards.*, card_batches.target_tag_id,
                       card_batches.delivery_mode, card_batches.address_mode AS batch_address_mode,
                       card_batches.source_scope, card_batches.include_tag_ids,
                       card_batches.exclude_tag_ids,
                       categories.name AS target_tag_name,
                       categories.alias_use_limit AS target_alias_use_limit
                FROM redemption_cards
                JOIN card_batches ON card_batches.id = redemption_cards.batch_id
                JOIN categories ON categories.id = card_batches.target_tag_id
                WHERE redemption_cards.code = ?
                """,
                (normalized_code,),
            ).fetchone()
            if card is None:
                connection.rollback()
                raise ValueError("card not found")

            active = connection.execute(
                "SELECT id FROM registration_claims WHERE card_id = ? AND status = 'pending'",
                (int(card["id"]),),
            ).fetchone()
            if active is not None:
                connection.commit()
                existing = self.get_registration_claim(int(active["id"]))
                if existing is None:
                    raise ValueError("registration claim not found")
                return existing

            if str(card["status"]) != "active":
                connection.rollback()
                raise ValueError("card is disabled")
            if int(card["remaining_uses"]) <= 0:
                connection.rollback()
                raise ValueError("card has no remaining uses")
            expires_at = str(card["expires_at"] or "")
            if expires_at and expires_at <= now:
                connection.rollback()
                raise ValueError("card is expired")
            cooldown_until = str(card["cooldown_until"] or "")
            if cooldown_until and cooldown_until > now:
                connection.rollback()
                raise ValueError("card is cooling down")

            batch_mode = str(card["batch_address_mode"])
            if batch_mode == "choice":
                actual_mode = requested_mode or "primary"
            else:
                actual_mode = batch_mode
                if requested_mode is not None and requested_mode != actual_mode:
                    connection.rollback()
                    raise ValueError("address mode is not allowed by this card")
            delivery_mode = str(card["delivery_mode"])
            if delivery_mode == "independent" and actual_mode != "primary":
                connection.rollback()
                raise ValueError("independent delivery only supports primary addresses")
            target_site_label = (
                "独立邮箱" if delivery_mode == "independent" else str(card["target_tag_name"])
            )

            clauses = [
                "m.address_kind = 'primary'",
                "m.parent_mapping_id = 0",
                "m.reuse_policy = 'reusable'",
                # RFC 保留示例域名只应用于文档和测试，不能作为公开兑换库存发放。
                "LOWER(SUBSTR(m.recipient_email, INSTR(m.recipient_email, '@') + 1)) "
                "NOT IN ('example.com', 'example.net', 'example.org', 'localhost')",
                "LOWER(SUBSTR(m.recipient_email, INSTR(m.recipient_email, '@') + 1)) "
                "NOT LIKE '%.example.com'",
                "LOWER(SUBSTR(m.recipient_email, INSTR(m.recipient_email, '@') + 1)) "
                "NOT LIKE '%.example.net'",
                "LOWER(SUBSTR(m.recipient_email, INSTR(m.recipient_email, '@') + 1)) "
                "NOT LIKE '%.example.org'",
                "LOWER(SUBSTR(m.recipient_email, INSTR(m.recipient_email, '@') + 1)) "
                "NOT LIKE '%.invalid'",
                "LOWER(SUBSTR(m.recipient_email, INSTR(m.recipient_email, '@') + 1)) "
                "NOT LIKE '%.test'",
                "NOT EXISTS (SELECT 1 FROM mapping_tags protected_tag "
                "JOIN categories protected_category ON protected_category.id = protected_tag.tag_id "
                "WHERE protected_tag.mapping_id = m.id AND protected_category.prevents_reuse = 1)",
                "m.status = 'idle'",
                "NOT EXISTS (SELECT 1 FROM access_mappings active_alias "
                "WHERE active_alias.parent_mapping_id = m.id "
                "AND active_alias.status = 'in_progress')",
                "NOT EXISTS (SELECT 1 FROM registration_claims active_claim "
                "WHERE active_claim.root_mapping_id = m.id AND active_claim.status = 'pending')",
            ]
            params: list[object] = []
            if actual_mode == "icloud_alias":
                clauses.extend(
                    [
                        "LOWER(m.recipient_email) LIKE '%@icloud.com'",
                        "INSTR(SUBSTR(m.recipient_email, 1, INSTR(m.recipient_email, '@') - 1), '+') = 0",
                    ]
                )
                alias_use_limit = max(0, int(card["target_alias_use_limit"] or 0))
                if alias_use_limit:
                    clauses.append(
                        "(SELECT COUNT(*) FROM alias_generation_events alias_history "
                        "WHERE alias_history.root_mapping_id = m.id "
                        "AND alias_history.tag_id = ?) < ?"
                    )
                    params.extend((int(card["target_tag_id"]), alias_use_limit))
            else:
                # 主邮箱被当前卡跳过或等待超时后，应继续分配下一条，避免用户
                # 连续看到同一个地址。裂变模式不加此限制，因为它会在同一主邮箱
                # 下创建一个从未发放过的新别名。
                clauses.append(
                    "NOT EXISTS (SELECT 1 FROM registration_claims ended_claim "
                    "WHERE ended_claim.card_id = ? AND ended_claim.root_mapping_id = m.id "
                    "AND ended_claim.status IN ('skipped', 'timed_out'))"
                )
                params.append(int(card["id"]))
                clauses.append(
                    "NOT EXISTS (SELECT 1 FROM mapping_tags target_usage "
                    "WHERE target_usage.mapping_id = m.id AND target_usage.tag_id = ? "
                    "AND target_usage.source = 'usage')"
                )
                params.append(int(card["target_tag_id"]))
            source_scope = str(card["source_scope"] or "all_reusable")
            if source_scope == "never_used":
                clauses.extend(
                    [
                        "m.first_used_at = ''",
                        "NOT EXISTS (SELECT 1 FROM registration_claims source_claim "
                        "WHERE source_claim.root_mapping_id = m.id AND source_claim.status = 'completed')",
                    ]
                )
            elif source_scope == "used_reusable":
                clauses.append(
                    "(m.first_used_at != '' OR EXISTS (SELECT 1 FROM registration_claims source_claim "
                    "WHERE source_claim.root_mapping_id = m.id AND source_claim.status = 'completed'))"
                )
            for include_tag_id in self._decode_int_values(card["include_tag_ids"]):
                clauses.append(
                    "EXISTS (SELECT 1 FROM mapping_tags include_tag "
                    "WHERE include_tag.mapping_id = m.id AND include_tag.tag_id = ?)"
                )
                params.append(include_tag_id)
            for exclude_tag_id in self._decode_int_values(card["exclude_tag_ids"]):
                clauses.append(
                    "NOT EXISTS (SELECT 1 FROM mapping_tags exclude_tag "
                    "WHERE exclude_tag.mapping_id = m.id AND exclude_tag.tag_id = ?)"
                )
                params.append(exclude_tag_id)
            if delivery_mode == "independent":
                clauses.extend(
                    [
                        "m.first_used_at = ''",
                        "NOT EXISTS (SELECT 1 FROM mapping_tags existing_tag "
                        "WHERE existing_tag.mapping_id = m.id AND existing_tag.source = 'usage')",
                        "NOT EXISTS (SELECT 1 FROM registration_claims completed_claim "
                        "WHERE completed_claim.root_mapping_id = m.id AND completed_claim.status = 'completed')",
                        "NOT EXISTS (SELECT 1 FROM access_mappings child_alias "
                        "WHERE child_alias.parent_mapping_id = m.id)",
                    ]
                )

            candidate = connection.execute(
                f"""
                SELECT m.*
                FROM access_mappings m
                WHERE {' AND '.join(clauses)}
                ORDER BY
                    CASE WHEN m.first_used_at != '' OR EXISTS(
                        SELECT 1 FROM registration_claims used_claim
                        WHERE used_claim.root_mapping_id = m.id AND used_claim.status = 'completed'
                    ) THEN 0 ELSE 1 END,
                    m.id ASC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
            if candidate is None:
                connection.rollback()
                raise ValueError("no available mapping")

            root_mapping_id = int(candidate["id"])
            if actual_mode == "icloud_alias":
                mapping_id = self._insert_icloud_alias(
                    connection,
                    candidate,
                    alias_tag=alias_tag,
                    target_site=target_site_label,
                )
            else:
                mapping_id = root_mapping_id

            claimed_by = f"card:{int(card['id'])}"
            cursor = connection.execute(
                """
                UPDATE access_mappings
                SET status = 'in_progress', claimed_at = ?, claimed_by = ?, target_site = ?
                WHERE id = ? AND status = 'idle'
                """,
                (now, claimed_by, target_site_label, root_mapping_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ValueError("mapping is no longer available")
            # CloudMail 可能把 ``+tag`` 归一化成主邮箱。同一邮箱族一旦发放新地址，
            # 旧的主邮箱或裂变领取都不能再安全区分后续邮件，因此只保留历史码，
            # 并把实时轮询权原子转交给即将创建的新领取。
            connection.execute(
                """
                UPDATE registration_claims
                SET superseded_at = ?
                WHERE root_mapping_id = ?
                  AND status = 'completed'
                  AND superseded_at = ''
                """,
                (now, root_mapping_id),
            )
            claim_cursor = connection.execute(
                """
                INSERT INTO registration_claims (
                    card_id, mapping_id, root_mapping_id, target_tag_id, status,
                    address_mode, verification_code, email_id, baseline_email_id,
                    baseline_ready, view_token, created_at, completed_at, ended_at, revoked_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?, '', 0, ?, ?, ?, ?, '', '', '')
                """,
                (
                    int(card["id"]),
                    mapping_id,
                    root_mapping_id,
                    int(card["target_tag_id"]),
                    actual_mode,
                    max(
                        0,
                        int(baseline_email_id),
                        int(candidate["last_seen_email_id"] or 0),
                    ),
                    0 if defer_email_baseline else 1,
                    secrets.token_urlsafe(24),
                    now,
                ),
            )
            claim_id = int(claim_cursor.lastrowid)
            connection.commit()

        claim = self.get_registration_claim(claim_id)
        if claim is None:
            raise ValueError("registration claim not found")
        return claim

    def get_registration_claim(self, claim_id: int) -> RegistrationClaim | None:
        with self._connect() as connection:
            row = connection.execute(
                self._registration_claim_select() + " WHERE registration_claims.id = ?",
                (int(claim_id),),
            ).fetchone()
        return None if row is None else self._row_to_registration_claim(row)

    def update_registration_claim_baseline(
        self,
        claim_id: int,
        baseline_email_id: int,
    ) -> RegistrationClaim:
        """原子完成公开领取快照；并发重试不会覆盖已经生效的基线。"""

        normalized_baseline = max(0, int(baseline_email_id))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE registration_claims
                SET baseline_email_id = MAX(baseline_email_id, ?),
                    baseline_ready = 1
                WHERE id = ? AND status = 'pending' AND baseline_ready = 0
                """,
                (normalized_baseline, int(claim_id)),
            )
            connection.commit()
        claim = self.get_registration_claim(int(claim_id))
        if claim is None:
            raise ValueError("registration claim not found")
        if cursor.rowcount != 1 and (claim.status != "pending" or not claim.baseline_ready):
            raise ValueError("registration claim is not pending")
        return claim

    def get_registration_claim_by_token(
        self,
        claim_id: int,
        view_token: str,
    ) -> RegistrationClaim | None:
        normalized_token = (view_token or "").strip()
        if not normalized_token:
            return None
        claim = self.get_registration_claim(claim_id)
        if claim is None or not claim.view_token or claim.revoked_at:
            return None
        if not hmac.compare_digest(claim.view_token, normalized_token):
            return None
        return claim

    def revoke_registration_claim(self, claim_id: int) -> RegistrationClaim:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM registration_claims WHERE id = ?",
                (int(claim_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ValueError("registration claim not found")
            if str(row["status"]) == "pending":
                self._release_claim_mapping(connection, row, retire_alias=True)
                connection.execute(
                    "UPDATE registration_claims SET status = 'timed_out', ended_at = ? WHERE id = ?",
                    (now, int(claim_id)),
                )
            connection.execute(
                "UPDATE registration_claims SET revoked_at = ?, view_token = ? WHERE id = ?",
                (now, secrets.token_urlsafe(24), int(claim_id)),
            )
            connection.commit()
        claim = self.get_registration_claim(int(claim_id))
        if claim is None:
            raise ValueError("registration claim not found")
        return claim

    def get_pending_card_claim(self, card_code: str) -> RegistrationClaim | None:
        normalized_code = self._normalize_card_code(card_code)
        if not normalized_code:
            return None
        with self._connect() as connection:
            row = connection.execute(
                self._registration_claim_select()
                + " WHERE redemption_cards.code = ? AND registration_claims.status = 'pending'",
                (normalized_code,),
            ).fetchone()
        return None if row is None else self._row_to_registration_claim(row)

    def list_card_claims(
        self,
        card_code: str,
        *,
        include_ended: bool = True,
        limit: int = 50,
    ) -> list[RegistrationClaim]:
        normalized_code = self._normalize_card_code(card_code)
        if not normalized_code:
            raise ValueError("card code is required")
        status_clause = "" if include_ended else " AND registration_claims.status IN ('pending', 'completed')"
        with self._connect() as connection:
            card = connection.execute(
                "SELECT id FROM redemption_cards WHERE code = ?",
                (normalized_code,),
            ).fetchone()
            if card is None:
                raise ValueError("card not found")
            rows = connection.execute(
                self._registration_claim_select()
                + f" WHERE registration_claims.card_id = ?{status_clause}"
                + " ORDER BY registration_claims.id DESC LIMIT ?",
                (int(card["id"]), max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._row_to_registration_claim(row) for row in rows]

    def list_registration_claims(
        self,
        *,
        batch_id: int | None = None,
        limit: int = 100,
    ) -> list[RegistrationClaim]:
        """列出近期领取记录，供管理员撤销浏览器长期接码凭证。"""

        where = ""
        params: list[object] = []
        if batch_id is not None:
            where = " WHERE redemption_cards.batch_id = ?"
            params.append(int(batch_id))
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(
                self._registration_claim_select()
                + where
                + " ORDER BY registration_claims.id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._row_to_registration_claim(row) for row in rows]

    def complete_registration_claim(
        self,
        claim_id: int,
        *,
        card_code: str,
        verification_code: str,
        email_id: int = 0,
    ) -> RegistrationClaim:
        normalized_card_code = self._normalize_card_code(card_code)
        normalized_verification_code = verification_code.strip()
        if not normalized_card_code:
            raise ValueError("card code is required")
        if not normalized_verification_code:
            raise ValueError("verification code is required")
        now = self._now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                """
                SELECT registration_claims.*, redemption_cards.code, redemption_cards.remaining_uses,
                       card_batches.delivery_mode, categories.name AS target_tag_name,
                       access_mappings.recipient_email AS claimed_recipient_email
                FROM registration_claims
                JOIN redemption_cards ON redemption_cards.id = registration_claims.card_id
                JOIN card_batches ON card_batches.id = redemption_cards.batch_id
                JOIN categories ON categories.id = registration_claims.target_tag_id
                JOIN access_mappings ON access_mappings.id = registration_claims.mapping_id
                WHERE registration_claims.id = ?
                """,
                (int(claim_id),),
            ).fetchone()
            if claim is None or str(claim["code"]) != normalized_card_code:
                connection.rollback()
                raise ValueError("registration claim not found")
            if str(claim["status"]) == "completed":
                connection.commit()
                completed = self.get_registration_claim(int(claim_id))
                if completed is None:
                    raise ValueError("registration claim not found")
                return completed
            if str(claim["status"]) != "pending":
                connection.rollback()
                raise ValueError("registration claim is not pending")
            if not bool(int(claim["baseline_ready"] or 0)):
                connection.rollback()
                raise ValueError("registration claim baseline is not ready")
            if int(claim["remaining_uses"]) <= 0:
                connection.rollback()
                raise ValueError("card has no remaining uses")

            root_mapping_id = int(claim["root_mapping_id"])
            mapping_id = int(claim["mapping_id"])
            target_tag_id = int(claim["target_tag_id"])
            target_site_label = (
                "独立邮箱"
                if str(claim["delivery_mode"]) == "independent"
                else str(claim["target_tag_name"])
            )
            if str(claim["delivery_mode"]) == "custom":
                connection.execute(
                    """
                    INSERT INTO mapping_tags (mapping_id, tag_id, source, created_at)
                    VALUES (?, ?, 'usage', ?)
                    ON CONFLICT(mapping_id, tag_id) DO UPDATE SET source = 'usage'
                    """,
                    (root_mapping_id, target_tag_id, now),
                )
            connection.execute(
                """
                UPDATE access_mappings
                SET first_used_at = CASE WHEN first_used_at = '' THEN ? ELSE first_used_at END,
                    used_at = ?, status = 'idle', claimed_at = '', claimed_by = '',
                    target_site = ?,
                    last_seen_email_id = MAX(last_seen_email_id, ?),
                    reuse_policy = CASE WHEN ? = 'independent' THEN 'independent' ELSE reuse_policy END
                WHERE id = ?
                """,
                (
                    now,
                    now,
                    target_site_label,
                    max(0, int(email_id)),
                    str(claim["delivery_mode"]),
                    root_mapping_id,
                ),
            )
            if mapping_id != root_mapping_id:
                connection.execute(
                    """
                    UPDATE access_mappings
                    SET first_used_at = CASE WHEN first_used_at = '' THEN ? ELSE first_used_at END,
                        used_at = ?, target_site = ?,
                        last_seen_email_id = MAX(last_seen_email_id, ?)
                    WHERE id = ?
                    """,
                    (now, now, target_site_label, max(0, int(email_id)), mapping_id),
                )
            connection.execute(
                """
                UPDATE registration_claims
                SET status = 'completed', verification_code = ?, email_id = ?,
                    completed_at = ?, ended_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    normalized_verification_code,
                    max(0, int(email_id)),
                    now,
                    now,
                    int(claim_id),
                ),
            )
            connection.execute(
                """
                UPDATE redemption_cards
                SET remaining_uses = remaining_uses - 1,
                    consecutive_skips = 0,
                    cooldown_until = '',
                    updated_at = ?
                WHERE id = ? AND remaining_uses > 0
                """,
                (now, int(claim["card_id"])),
            )
            self._insert_verification_event(
                connection,
                root_mapping_id=root_mapping_id,
                mapping_id=mapping_id,
                tag_id=target_tag_id,
                source="public_card",
                claim_id=int(claim_id),
                email_id=max(0, int(email_id)),
                recipient_email=str(claim["claimed_recipient_email"]),
                address_mode=str(claim["address_mode"]),
                occurred_at=now,
            )
            connection.commit()

        completed = self.get_registration_claim(int(claim_id))
        if completed is None:
            raise ValueError("registration claim not found")
        return completed

    def record_registration_claim_code(
        self,
        claim_id: int,
        *,
        verification_code: str,
        email_id: int,
    ) -> RegistrationClaim:
        """为仍拥有实时轮询权的已完成领取记录后续验证码。

        后续接码不再次消耗卡次数；同一 CloudMail 邮件 ID 只写一条流水。
        ``+tag`` 别名被新别名取代后会拒绝继续取信，防止把新别名邮件串给旧领取。
        """

        normalized_code = verification_code.strip()
        normalized_email_id = int(email_id)
        if not normalized_code:
            raise ValueError("verification code is required")
        if normalized_email_id <= 0:
            raise ValueError("email_id must be positive")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                """
                SELECT registration_claims.*, access_mappings.recipient_email
                FROM registration_claims
                JOIN access_mappings ON access_mappings.id = registration_claims.mapping_id
                WHERE registration_claims.id = ?
                """,
                (int(claim_id),),
            ).fetchone()
            if claim is None:
                connection.rollback()
                raise ValueError("registration claim not found")
            if str(claim["status"]) != "completed":
                connection.rollback()
                raise ValueError("registration claim is not completed")
            if str(claim["revoked_at"] or ""):
                connection.rollback()
                raise ValueError("registration claim is revoked")
            if str(claim["superseded_at"] or ""):
                connection.rollback()
                raise ValueError("registration claim is superseded")

            previous_email_id = int(claim["email_id"] or 0)
            if normalized_email_id >= previous_email_id:
                connection.execute(
                    """
                    UPDATE registration_claims
                    SET verification_code = ?, email_id = ?
                    WHERE id = ? AND status = 'completed'
                    """,
                    (normalized_code, normalized_email_id, int(claim_id)),
                )
                connection.execute(
                    """
                    UPDATE access_mappings
                    SET last_seen_email_id = MAX(last_seen_email_id, ?)
                    WHERE id IN (?, ?)
                    """,
                    (
                        normalized_email_id,
                        int(claim["mapping_id"]),
                        int(claim["root_mapping_id"]),
                    ),
                )
            if normalized_email_id >= previous_email_id:
                self._insert_verification_event(
                    connection,
                    root_mapping_id=int(claim["root_mapping_id"]),
                    mapping_id=int(claim["mapping_id"]),
                    tag_id=int(claim["target_tag_id"]),
                    source="public_card",
                    claim_id=int(claim_id),
                    email_id=normalized_email_id,
                    recipient_email=str(claim["recipient_email"]),
                    address_mode=str(claim["address_mode"]),
                    occurred_at=now,
                )
            connection.commit()

        updated = self.get_registration_claim(int(claim_id))
        if updated is None:
            raise ValueError("registration claim not found")
        return updated

    def skip_registration_claim(
        self,
        claim_id: int,
        *,
        card_code: str,
        skip_limit: int = 3,
        cooldown_minutes: int = 15,
    ) -> RegistrationClaim:
        normalized_card_code = self._normalize_card_code(card_code)
        if not normalized_card_code:
            raise ValueError("card code is required")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                """
                SELECT registration_claims.*, redemption_cards.code
                FROM registration_claims
                JOIN redemption_cards ON redemption_cards.id = registration_claims.card_id
                WHERE registration_claims.id = ?
                """,
                (int(claim_id),),
            ).fetchone()
            if claim is None or str(claim["code"]) != normalized_card_code:
                connection.rollback()
                raise ValueError("registration claim not found")
            if str(claim["status"]) != "pending":
                connection.commit()
                existing = self.get_registration_claim(int(claim_id))
                if existing is None:
                    raise ValueError("registration claim not found")
                return existing

            connection.execute(
                "UPDATE registration_claims SET status = 'skipped', ended_at = ? WHERE id = ?",
                (now, int(claim_id)),
            )
            self._release_claim_mapping(connection, claim, retire_alias=True)
            self._register_card_skip(
                connection,
                int(claim["card_id"]),
                now,
                skip_limit=max(1, int(skip_limit)),
                cooldown_minutes=max(1, int(cooldown_minutes)),
            )
            connection.commit()

        skipped = self.get_registration_claim(int(claim_id))
        if skipped is None:
            raise ValueError("registration claim not found")
        return skipped

    def expire_registration_claims(self, timeout_minutes: int = 30) -> int:
        normalized_timeout = int(timeout_minutes)
        if normalized_timeout <= 0:
            raise ValueError("timeout_minutes must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = self._expire_pending_claims(
                connection,
                now=self._now(),
                timeout_minutes=normalized_timeout,
            )
            connection.commit()
        return expired

    def delete_mappings(self, mapping_ids: list[int]) -> int:
        normalized_ids = sorted({int(mapping_id) for mapping_id in mapping_ids})
        if not normalized_ids:
            raise ValueError("mapping_ids is required")

        placeholders = ", ".join("?" for _ in normalized_ids)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                selected_rows = connection.execute(
                    f"SELECT id, address_kind FROM access_mappings WHERE id IN ({placeholders})",
                    tuple(normalized_ids),
                ).fetchall()
                if not selected_rows:
                    connection.commit()
                    return 0

                delete_ids = {int(row["id"]) for row in selected_rows}
                root_ids = [
                    int(row["id"])
                    for row in selected_rows
                    if str(row["address_kind"] or "primary") == "primary"
                ]
                if root_ids:
                    root_placeholders = ", ".join("?" for _ in root_ids)
                    child_rows = connection.execute(
                        f"SELECT id FROM access_mappings WHERE parent_mapping_id IN ({root_placeholders})",
                        tuple(root_ids),
                    ).fetchall()
                    delete_ids.update(int(row["id"]) for row in child_rows)

                delete_placeholders = ", ".join("?" for _ in delete_ids)
                delete_params = tuple(sorted(delete_ids))
                referenced_claim = connection.execute(
                    f"""
                    SELECT id
                    FROM registration_claims
                    WHERE mapping_id IN ({delete_placeholders})
                       OR root_mapping_id IN ({delete_placeholders})
                    LIMIT 1
                    """,
                    delete_params + delete_params,
                ).fetchone()
                if referenced_claim is not None:
                    connection.rollback()
                    raise ValueError("mapping has registration history")

                cursor = connection.execute(
                    f"DELETE FROM access_mappings WHERE id IN ({delete_placeholders})",
                    delete_params,
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            # 注册记录必须长期保留，以保证已完成领取的查看令牌持续可用。
            raise ValueError("mapping has registration history") from exc

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
                       used_at, last_seen_email_id, target_site, address_kind, parent_mapping_id,
                       alias_tag, reuse_policy, first_used_at, claim_source_tag_id
                FROM access_mappings
                WHERE {where_clause}
                ORDER BY claimed_at DESC, id ASC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()

        return self._attach_mapping_tags(self._row_to_mapping(row))

    def claim_next_available_mapping(
        self,
        category_filter: str = "",
        target_site: str = "",
        claimed_by: str = "",
        after_mapping_id: int | None = None,
        address_mode: str = "primary",
        exclude_tag_id: int | None = None,
        defer_email_baseline: bool = False,
    ) -> AccessMapping | None:
        normalized_category = self._category_key(category_filter)
        normalized_target_site = target_site.strip()
        normalized_claimed_by = claimed_by.strip()
        normalized_address_mode = (address_mode or "primary").strip().lower()
        effective_exclude_tag_id = exclude_tag_id
        if effective_exclude_tag_id is None and normalized_target_site:
            effective_exclude_tag_id = self.get_category_id(normalized_target_site)
        alias_use_limit = 0
        if normalized_address_mode == "icloud_alias" and effective_exclude_tag_id is not None:
            target_tag = self.get_tag(int(effective_exclude_tag_id))
            if target_tag is not None:
                alias_use_limit = target_tag.alias_use_limit
        claimed_at = self._now()

        if not normalized_claimed_by:
            raise ValueError("claimed_by is required")
        if normalized_address_mode not in VALID_ADDRESS_MODES:
            raise ValueError("invalid address mode")

        where_clause = (
            "status = ? AND address_kind = 'primary' AND reuse_policy = 'reusable' "
            "AND NOT EXISTS (SELECT 1 FROM mapping_tags protected_tag "
            "JOIN categories protected_category ON protected_category.id = protected_tag.tag_id "
            "WHERE protected_tag.mapping_id = access_mappings.id "
            "AND protected_category.prevents_reuse = 1) "
            "AND NOT EXISTS (SELECT 1 FROM access_mappings active_alias "
            "WHERE active_alias.parent_mapping_id = access_mappings.id "
            "AND active_alias.status = 'in_progress')"
        )
        params: list[object] = ["idle"]
        if normalized_category:
            where_clause = (
                f"{where_clause} AND EXISTS (SELECT 1 FROM mapping_tags source_tag "
                "JOIN categories source_category ON source_category.id = source_tag.tag_id "
                "WHERE source_tag.mapping_id = access_mappings.id "
                "AND source_category.normalized_name = ?)"
            )
            params.append(normalized_category)
        if after_mapping_id is not None:
            where_clause = f"{where_clause} AND id > ?"
            params.append(after_mapping_id)
        if normalized_address_mode == "icloud_alias":
            where_clause = (
                f"{where_clause} AND LOWER(recipient_email) LIKE '%@icloud.com' "
                "AND INSTR(SUBSTR(recipient_email, 1, INSTR(recipient_email, '@') - 1), '+') = 0"
            )
            if alias_use_limit:
                where_clause = (
                    f"{where_clause} AND (SELECT COUNT(*) FROM alias_generation_events alias_history "
                    "WHERE alias_history.root_mapping_id = access_mappings.id "
                    "AND alias_history.tag_id = ?) < ?"
                )
                params.extend((int(effective_exclude_tag_id), alias_use_limit))
        elif effective_exclude_tag_id is not None:
            where_clause = (
                f"{where_clause} AND NOT EXISTS (SELECT 1 FROM mapping_tags excluded_usage "
                "WHERE excluded_usage.mapping_id = access_mappings.id "
                "AND excluded_usage.tag_id = ? AND excluded_usage.source = 'usage')"
            )
            params.append(int(effective_exclude_tag_id))

        try:
            with self._connect() as connection:
                # 先取得 SQLite 写锁，再在同一事务中检查并领取，保证同一调用方并发重试时
                # 最多只会拥有一条 in_progress 记录。
                connection.execute("BEGIN IMMEDIATE")
                current_row = connection.execute(
                    """
                    SELECT id, recipient_email, query_email, access_key, label, category, created_at, status,
                           claimed_at, claimed_by, used_at, last_seen_email_id, target_site,
                           address_kind, parent_mapping_id, alias_tag, reuse_policy, first_used_at,
                           claim_source_tag_id
                    FROM access_mappings
                    WHERE status = 'in_progress' AND claimed_by = ?
                    ORDER BY claimed_at DESC, id ASC
                    LIMIT 1
                    """,
                    (normalized_claimed_by,),
                ).fetchone()
                if current_row is not None:
                    connection.commit()
                    return self._attach_mapping_tags(self._row_to_mapping(current_row))

                row = connection.execute(
                    f"""
                    SELECT *
                    FROM access_mappings
                    WHERE {where_clause}
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    tuple(params),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None

                root_mapping_id = int(row["id"])
                source_tag_id = 0
                if normalized_category:
                    source_tag_row = connection.execute(
                        "SELECT id FROM categories WHERE normalized_name = ?",
                        (normalized_category,),
                    ).fetchone()
                    if source_tag_row is not None:
                        source_tag_id = int(source_tag_row["id"])
                if normalized_address_mode == "icloud_alias":
                    mapping_id = self._insert_icloud_alias(
                        connection,
                        row,
                        alias_tag=None,
                        target_site=normalized_target_site,
                    )
                else:
                    mapping_id = root_mapping_id
                # 后台/API 一旦领取该邮箱族，旧公开领取便无法再与新邮件安全区分。
                # 在同一写事务中冻结旧凭证，确保一个主邮箱族始终只有一个实时接码方。
                connection.execute(
                    """
                    UPDATE registration_claims
                    SET superseded_at = ?
                    WHERE root_mapping_id = ?
                      AND status = 'completed'
                      AND superseded_at = ''
                    """,
                    (claimed_at, root_mapping_id),
                )
                connection.execute(
                    """
                    UPDATE access_mappings
                    SET status = 'in_progress',
                        claimed_at = ?,
                        claimed_by = ?,
                        used_at = '',
                        claim_baseline_ready = ?,
                        claim_source_tag_id = ?,
                        target_site = CASE WHEN ? != '' THEN ? ELSE target_site END
                    WHERE id = ? AND status = 'idle'
                    """,
                    (
                        claimed_at,
                        normalized_claimed_by,
                        0 if defer_email_baseline else 1,
                        source_tag_id,
                        normalized_target_site,
                        normalized_target_site,
                        mapping_id,
                    ),
                )
                claimed_row = connection.execute(
                    """
                    SELECT id, recipient_email, query_email, access_key, label, category, created_at, status,
                           claimed_at, claimed_by, used_at, last_seen_email_id, target_site,
                           address_kind, parent_mapping_id, alias_tag, reuse_policy, first_used_at,
                           claim_source_tag_id
                    FROM access_mappings
                    WHERE id = ?
                    """,
                    (mapping_id,),
                ).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("claimed_by already has active mapping") from exc

        return self._attach_mapping_tags(self._row_to_mapping(claimed_row))

    def is_workbench_claim_baseline_ready(self, mapping_id: int, *, claimed_by: str) -> bool:
        """检查当前工作台领取是否已经完成邮件快照。"""

        normalized_claimed_by = claimed_by.strip()
        if not normalized_claimed_by:
            raise ValueError("claimed_by is required")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT claim_baseline_ready
                FROM access_mappings
                WHERE id = ? AND status = 'in_progress' AND claimed_by = ?
                """,
                (int(mapping_id), normalized_claimed_by),
            ).fetchone()
        if row is None:
            raise ValueError("mapping not claimed by this session")
        return bool(int(row["claim_baseline_ready"] or 0))

    def finalize_workbench_claim_baseline(
        self,
        mapping_id: int,
        *,
        claimed_by: str,
        baseline_email_id: int,
    ) -> AccessMapping:
        """原子完成工作台领取快照，并同步该邮箱族的邮件编号水位。"""

        normalized_claimed_by = claimed_by.strip()
        normalized_baseline = max(0, int(baseline_email_id))
        if not normalized_claimed_by:
            raise ValueError("claimed_by is required")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, parent_mapping_id, claim_baseline_ready
                FROM access_mappings
                WHERE id = ? AND status = 'in_progress' AND claimed_by = ?
                """,
                (int(mapping_id), normalized_claimed_by),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ValueError("mapping not claimed by this session")

            if not bool(int(row["claim_baseline_ready"] or 0)):
                root_mapping_id = int(row["parent_mapping_id"] or row["id"])
                connection.execute(
                    """
                    UPDATE access_mappings
                    SET last_seen_email_id = MAX(last_seen_email_id, ?),
                        claim_baseline_ready = 1
                    WHERE id = ? AND status = 'in_progress' AND claimed_by = ?
                    """,
                    (normalized_baseline, int(mapping_id), normalized_claimed_by),
                )
                if root_mapping_id != int(mapping_id):
                    connection.execute(
                        """
                        UPDATE access_mappings
                        SET last_seen_email_id = MAX(last_seen_email_id, ?)
                        WHERE id = ?
                        """,
                        (normalized_baseline, root_mapping_id),
                    )
            connection.commit()

        mapping = self.get_by_id(int(mapping_id))
        if mapping is None:
            raise ValueError("mapping not found")
        return mapping

    def bind_workbench_target_tag(
        self,
        mapping_id: int,
        *,
        claimed_by: str,
        target_tag_id: int,
    ) -> AccessMapping:
        """为旧领取补绑定平台；已有平台时只允许同一标签重复确认。"""

        normalized_claimed_by = claimed_by.strip()
        if not normalized_claimed_by:
            raise ValueError("claimed_by is required")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            mapping = connection.execute(
                """
                SELECT id, target_site
                FROM access_mappings
                WHERE id = ? AND status = 'in_progress' AND claimed_by = ?
                """,
                (int(mapping_id), normalized_claimed_by),
            ).fetchone()
            if mapping is None:
                connection.rollback()
                raise ValueError("mapping not claimed by this session")

            target_tag = connection.execute(
                """
                SELECT id, name
                FROM categories
                WHERE id = ? AND archived = 0 AND kind = 'service'
                """,
                (int(target_tag_id),),
            ).fetchone()
            if target_tag is None:
                connection.rollback()
                raise ValueError("tag not found")

            current_target = str(mapping["target_site"] or "").strip()
            target_name = str(target_tag["name"])
            if current_target and self._category_key(current_target) != self._category_key(target_name):
                connection.rollback()
                raise ValueError("target tag does not match claimed mapping")
            if not current_target:
                connection.execute(
                    """
                    UPDATE access_mappings
                    SET target_site = ?
                    WHERE id = ? AND status = 'in_progress' AND claimed_by = ?
                    """,
                    (target_name, int(mapping_id), normalized_claimed_by),
                )
            connection.commit()

        updated = self.get_by_id(int(mapping_id))
        if updated is None:
            raise ValueError("mapping not found")
        return updated

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
                        claim_baseline_ready = 1,
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
                        claim_baseline_ready = 0,
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
        *,
        target_tag_id: int,
        claimed_by: str,
        verification_source: str,
        email_id: int,
        prevent_reuse: bool = False,
    ) -> AccessMapping:
        normalized_claimed_by = claimed_by.strip()
        normalized_source = verification_source.strip().lower()
        normalized_email_id = int(email_id)
        completed_at = self._now()

        if not normalized_claimed_by:
            raise ValueError("claimed_by is required")
        if normalized_source not in {"admin_workbench", "external_api"}:
            raise ValueError("invalid verification event source")
        if normalized_email_id <= 0:
            raise ValueError("email_id must be positive")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            mapping_row = connection.execute(
                """
                SELECT id, parent_mapping_id, target_site, recipient_email,
                       address_kind, claimed_at, claim_baseline_ready
                FROM access_mappings
                WHERE id = ? AND status = 'in_progress' AND claimed_by = ?
                """,
                (mapping_id, normalized_claimed_by),
            ).fetchone()
            if mapping_row is None:
                connection.rollback()
                raise ValueError("mapping not claimed by this session")
            if not bool(int(mapping_row["claim_baseline_ready"] or 0)):
                connection.rollback()
                raise ValueError("workbench claim baseline is not ready")
            root_mapping_id = int(mapping_row["parent_mapping_id"] or mapping_row["id"])
            root_row = connection.execute(
                "SELECT first_used_at FROM access_mappings WHERE id = ?",
                (root_mapping_id,),
            ).fetchone()
            if root_row is None:
                connection.rollback()
                raise ValueError("mapping not found")
            target_tag_row = connection.execute(
                """
                SELECT id, name
                FROM categories
                WHERE id = ? AND archived = 0 AND kind = 'service'
                """,
                (int(target_tag_id),),
            ).fetchone()
            if target_tag_row is None:
                connection.rollback()
                raise ValueError("tag not found")
            target_tag_name = str(target_tag_row["name"])
            claimed_target_site = str(mapping_row["target_site"] or "")
            if claimed_target_site and self._category_key(claimed_target_site) != self._category_key(
                target_tag_name
            ):
                connection.rollback()
                raise ValueError("target tag does not match claimed mapping")
            cursor = connection.execute(
                """
                UPDATE access_mappings
                SET status = 'idle',
                    claimed_at = '',
                    claimed_by = '',
                    used_at = ?,
                    first_used_at = CASE WHEN first_used_at = '' THEN ? ELSE first_used_at END,
                    last_seen_email_id = MAX(last_seen_email_id, ?),
                    claim_baseline_ready = 1,
                    target_site = ?
                WHERE id = ? AND status = 'in_progress' AND claimed_by = ?
                """,
                (
                    completed_at,
                    completed_at,
                    normalized_email_id,
                    target_tag_name,
                    mapping_id,
                    normalized_claimed_by,
                ),
            )
            if cursor.rowcount:
                if prevent_reuse:
                    next_policy = "independent" if not str(root_row["first_used_at"] or "") else "retired"
                else:
                    next_policy = None
                connection.execute(
                    """
                    UPDATE access_mappings
                    SET used_at = ?,
                        first_used_at = CASE WHEN first_used_at = '' THEN ? ELSE first_used_at END,
                        last_seen_email_id = MAX(last_seen_email_id, ?),
                        reuse_policy = CASE WHEN ? IS NULL THEN reuse_policy ELSE ? END,
                        target_site = ?
                    WHERE id = ?
                    """,
                    (
                        completed_at,
                        completed_at,
                        normalized_email_id,
                        next_policy,
                        next_policy,
                        target_tag_name,
                        root_mapping_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO mapping_tags (mapping_id, tag_id, source, created_at)
                    VALUES (?, ?, 'usage', ?)
                    ON CONFLICT(mapping_id, tag_id) DO UPDATE SET source = 'usage'
                    """,
                    (root_mapping_id, int(target_tag_row["id"]), completed_at),
                )
                self._insert_verification_event(
                    connection,
                    root_mapping_id=root_mapping_id,
                    mapping_id=int(mapping_row["id"]),
                    tag_id=int(target_tag_row["id"]),
                    source=normalized_source,
                    email_id=normalized_email_id,
                    recipient_email=str(mapping_row["recipient_email"]),
                    address_mode=str(mapping_row["address_kind"] or "primary"),
                    occurred_at=completed_at,
                    fallback_event_key=(
                        f"workbench:{normalized_source}:{int(mapping_row['id'])}:"
                        f"{str(mapping_row['claimed_at'])}"
                    ),
                )
            connection.commit()

        if cursor.rowcount == 0:
            raise ValueError("mapping not claimed by this session")

        updated = self.get_by_id(mapping_id)
        if updated is None:
            raise ValueError("mapping not found")
        return updated

    def reset_mapping_status(self, mapping_id: int, claimed_by: str | None = None) -> AccessMapping:
        normalized_claimed_by = None if claimed_by is None else claimed_by.strip()
        if claimed_by is not None and not normalized_claimed_by:
            raise ValueError("claimed_by is required")

        where_clause = "id = ?"
        params: tuple[object, ...] = (mapping_id,)
        if normalized_claimed_by is not None:
            where_clause = f"{where_clause} AND status = 'in_progress' AND claimed_by = ?"
            params = (mapping_id, normalized_claimed_by)

        with self._connect() as connection:
            current = connection.execute(
                f"SELECT address_kind FROM access_mappings WHERE {where_clause}",
                params,
            ).fetchone()
            retire_alias = current is not None and str(current["address_kind"]) == "icloud_alias"
            cursor = connection.execute(
                f"""
                UPDATE access_mappings
                SET status = 'idle',
                    claimed_at = '',
                    claimed_by = '',
                    claim_baseline_ready = 1,
                    target_site = '',
                    reuse_policy = CASE WHEN ? THEN 'retired' ELSE reuse_policy END,
                    access_key = CASE WHEN ? THEN ? ELSE access_key END
                WHERE {where_clause}
                """,
                (
                    1 if retire_alias else 0,
                    1 if retire_alias else 0,
                    self._generate_key(),
                    *params,
                ),
            )
            connection.commit()

        if cursor.rowcount == 0:
            if self.get_by_id(mapping_id) is None:
                raise ValueError("mapping not found")
            if normalized_claimed_by is not None:
                raise ValueError("mapping not claimed by this session")
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

    def save_verification_extraction_settings(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout_seconds: int | str = 10,
        clear_api_key: bool = False,
        mode: str = "off",
        custom_patterns: str | list[str] | tuple[str, ...] = (),
    ) -> VerificationExtractionSettingsRecord:
        existing = self.get_verification_extraction_settings()
        normalized_mode = "off" if clear_api_key else (mode or "off").strip().lower()
        if normalized_mode not in VALID_GLOBAL_EXTRACTION_MODES:
            raise ValueError("verification extraction global mode is invalid")
        normalized_patterns = validate_custom_patterns(self._split_rule_lines(custom_patterns))
        if clear_api_key:
            normalized_base_url = ""
            normalized_api_key = ""
            normalized_model = ""
        else:
            normalized_base_url = validate_openai_base_url(base_url)
            normalized_api_key = api_key.strip() or existing.api_key
            normalized_model = model.strip()
        try:
            normalized_timeout = int(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("verification ai timeout is invalid") from exc
        if not 1 <= normalized_timeout <= 60:
            raise ValueError("verification ai timeout is invalid")
        configured_fields = (normalized_base_url, normalized_api_key, normalized_model)
        if any(configured_fields) and not all(configured_fields):
            raise ValueError("verification ai config is incomplete")
        if normalized_mode in {"fallback", "only"} and not all(configured_fields):
            raise ValueError("verification ai config is required")
        updated_at = self._now()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                [
                    ("verification_ai_base_url", normalized_base_url, updated_at),
                    ("verification_ai_api_key", normalized_api_key, updated_at),
                    ("verification_ai_model", normalized_model, updated_at),
                    ("verification_ai_timeout_seconds", str(normalized_timeout), updated_at),
                    ("verification_extraction_mode", normalized_mode, updated_at),
                    (
                        "verification_code_patterns",
                        json.dumps(normalized_patterns, ensure_ascii=False),
                        updated_at,
                    ),
                ],
            )
            connection.commit()
        return VerificationExtractionSettingsRecord(
            base_url=normalized_base_url,
            api_key=normalized_api_key,
            model=normalized_model,
            timeout_seconds=normalized_timeout,
            updated_at=updated_at,
            mode=normalized_mode,
            custom_patterns=normalized_patterns,
        )

    def get_verification_extraction_settings(
        self,
        *,
        default_base_url: str = "",
        default_api_key: str = "",
        default_model: str = "",
        default_timeout_seconds: int = 10,
        default_mode: str = "off",
        default_custom_patterns: tuple[str, ...] = (),
    ) -> VerificationExtractionSettingsRecord:
        keys = (
            "verification_ai_base_url",
            "verification_ai_api_key",
            "verification_ai_model",
            "verification_ai_timeout_seconds",
            "verification_extraction_mode",
            "verification_code_patterns",
        )
        placeholders = ", ".join("?" for _ in keys)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT key, value, updated_at FROM app_settings WHERE key IN ({placeholders})",
                keys,
            ).fetchall()
        values = {
            "verification_ai_base_url": default_base_url,
            "verification_ai_api_key": default_api_key,
            "verification_ai_model": default_model,
            "verification_ai_timeout_seconds": str(default_timeout_seconds),
            "verification_extraction_mode": default_mode,
            "verification_code_patterns": json.dumps(default_custom_patterns, ensure_ascii=False),
        }
        updated_at = ""
        for row in rows:
            values[str(row["key"])] = str(row["value"])
            updated_at = max(updated_at, str(row["updated_at"]))
        try:
            timeout = int(values["verification_ai_timeout_seconds"])
        except ValueError:
            timeout = 10
        mode = str(values["verification_extraction_mode"] or "off").strip().lower()
        if mode not in VALID_GLOBAL_EXTRACTION_MODES:
            mode = "off"
        return VerificationExtractionSettingsRecord(
            base_url=values["verification_ai_base_url"],
            api_key=values["verification_ai_api_key"],
            model=values["verification_ai_model"],
            timeout_seconds=max(1, min(timeout, 60)),
            updated_at=updated_at,
            mode=mode,
            custom_patterns=self._decode_rule_values(values["verification_code_patterns"]),
        )

    def _attach_mapping_tags(self, mapping: AccessMapping | None) -> AccessMapping | None:
        if mapping is None:
            return None
        with self._connect() as connection:
            detail = connection.execute(
                """
                SELECT address_kind, parent_mapping_id, alias_tag, reuse_policy, first_used_at,
                       claim_source_tag_id
                FROM access_mappings
                WHERE id = ?
                """,
                (mapping.id,),
            ).fetchone()
            if detail is None:
                return None
            mapping.address_kind = str(detail["address_kind"] or "primary")
            mapping.parent_mapping_id = int(detail["parent_mapping_id"] or 0)
            mapping.alias_tag = str(detail["alias_tag"] or "")
            mapping.reuse_policy = str(detail["reuse_policy"] or "reusable")
            mapping.first_used_at = str(detail["first_used_at"] or "")
            mapping.claim_source_tag_id = int(detail["claim_source_tag_id"] or 0)
            root_id = mapping.parent_mapping_id or mapping.id
            tag_rows = connection.execute(
                """
                SELECT categories.name
                FROM mapping_tags
                JOIN categories ON categories.id = mapping_tags.tag_id
                WHERE mapping_tags.mapping_id = ?
                ORDER BY categories.normalized_name ASC, categories.id ASC
                """,
                (root_id,),
            ).fetchall()
        mapping.tags = tuple(str(row["name"]) for row in tag_rows)
        return mapping

    @staticmethod
    def _row_to_tag(row: sqlite3.Row) -> TagOption:
        keys = set(row.keys())
        return TagOption(
            id=int(row["id"]),
            name=str(row["name"]),
            count=int(row["usage_count"] or 0),
            color=str(row["color"] or ""),
            archived=bool(row["archived"]),
            success_count=int(row["success_count"] or 0) if "success_count" in keys else 0,
            kind=str(row["kind"] or "service") if "kind" in keys else "service",
            sender_patterns=KeyStore._decode_rule_values(
                row["sender_patterns"] if "sender_patterns" in keys else ""
            ),
            subject_keywords=KeyStore._decode_rule_values(
                row["subject_keywords"] if "subject_keywords" in keys else ""
            ),
            prevents_reuse=bool(row["prevents_reuse"]) if "prevents_reuse" in keys else False,
            alias_use_limit=max(0, int(row["alias_use_limit"] or 0))
            if "alias_use_limit" in keys
            else 0,
            code_patterns=KeyStore._decode_rule_values(
                row["code_patterns"] if "code_patterns" in keys else ""
            ),
            extraction_mode=(
                str(row["extraction_mode"] or "rules")
                if "extraction_mode" in keys and str(row["extraction_mode"] or "rules") in VALID_EXTRACTION_MODES
                else "rules"
            ),
        )

    @staticmethod
    def _row_to_card_category(row: sqlite3.Row) -> CardCategory:
        return CardCategory(
            id=int(row["id"]),
            name=str(row["name"]),
            card_count=int(row["card_count"] or 0),
        )

    @staticmethod
    def _row_to_card_batch(row: sqlite3.Row) -> CardBatch:
        return CardBatch(
            id=int(row["id"]),
            name=str(row["name"]),
            category_id=int(row["category_id"]),
            category_name=str(row["category_name"]),
            target_tag_id=int(row["target_tag_id"]),
            target_tag_name=str(row["target_tag_name"]),
            delivery_mode=str(row["delivery_mode"]),
            address_mode=str(row["address_mode"]),
            uses_per_card=int(row["uses_per_card"]),
            card_count=int(row["card_count"] or 0),
            expires_at=str(row["expires_at"] or ""),
            created_at=str(row["created_at"]),
            source_scope=str(row["source_scope"] or "all_reusable")
            if "source_scope" in row.keys()
            else "all_reusable",
            include_tag_ids=KeyStore._decode_int_values(
                row["include_tag_ids"] if "include_tag_ids" in row.keys() else ""
            ),
            exclude_tag_ids=KeyStore._decode_int_values(
                row["exclude_tag_ids"] if "exclude_tag_ids" in row.keys() else ""
            ),
        )

    @staticmethod
    def _row_to_redemption_card(row: sqlite3.Row) -> RedemptionCard:
        return RedemptionCard(
            id=int(row["id"]),
            batch_id=int(row["batch_id"]),
            code=str(row["code"]),
            total_uses=int(row["total_uses"]),
            remaining_uses=int(row["remaining_uses"]),
            status=str(row["status"]),
            consecutive_skips=int(row["consecutive_skips"] or 0),
            cooldown_until=str(row["cooldown_until"] or ""),
            expires_at=str(row["expires_at"] or ""),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_to_registration_claim(row: sqlite3.Row) -> RegistrationClaim:
        return RegistrationClaim(
            id=int(row["id"]),
            card_id=int(row["card_id"]),
            mapping_id=int(row["mapping_id"]),
            root_mapping_id=int(row["root_mapping_id"]),
            target_tag_id=int(row["target_tag_id"]),
            status=str(row["status"]),
            address_mode=str(row["address_mode"]),
            recipient_email=str(row["recipient_email"]),
            access_key=str(row["access_key"] or ""),
            verification_code=str(row["verification_code"] or ""),
            email_id=int(row["email_id"] or 0),
            created_at=str(row["created_at"]),
            completed_at=str(row["completed_at"] or ""),
            ended_at=str(row["ended_at"] or ""),
            root_email=str(row["root_email"] or "") if "root_email" in row.keys() else "",
            query_email=str(row["query_email"] or "") if "query_email" in row.keys() else "",
            target_tag_name=str(row["target_tag_name"] or "")
            if "target_tag_name" in row.keys()
            else "",
            view_token=str(row["view_token"] or "") if "view_token" in row.keys() else "",
            baseline_email_id=int(row["baseline_email_id"] or 0)
            if "baseline_email_id" in row.keys()
            else 0,
            baseline_ready=bool(int(row["baseline_ready"] or 0))
            if "baseline_ready" in row.keys()
            else True,
            revoked_at=str(row["revoked_at"] or "") if "revoked_at" in row.keys() else "",
            superseded_at=str(row["superseded_at"] or "")
            if "superseded_at" in row.keys()
            else "",
        )

    @staticmethod
    def _row_to_verification_event(row: sqlite3.Row) -> VerificationEvent:
        return VerificationEvent(
            id=int(row["id"]),
            event_key=str(row["event_key"]),
            root_mapping_id=int(row["root_mapping_id"]),
            mapping_id=int(row["mapping_id"]),
            tag_id=int(row["tag_id"]),
            source=str(row["source"]),
            claim_id=int(row["claim_id"] or 0),
            email_id=int(row["email_id"] or 0),
            recipient_email=str(row["recipient_email"] or ""),
            address_mode=str(row["address_mode"] or "primary"),
            occurred_at=str(row["occurred_at"]),
        )

    @staticmethod
    def _card_batch_select() -> str:
        return """
            SELECT card_batches.*, card_categories.name AS category_name,
                   categories.name AS target_tag_name,
                   COUNT(redemption_cards.id) AS card_count
            FROM card_batches
            JOIN card_categories ON card_categories.id = card_batches.category_id
            JOIN categories ON categories.id = card_batches.target_tag_id
            LEFT JOIN redemption_cards ON redemption_cards.batch_id = card_batches.id
        """

    @staticmethod
    def _registration_claim_select() -> str:
        return """
            SELECT registration_claims.*, access_mappings.recipient_email,
                   '' AS access_key,
                   roots.recipient_email AS root_email,
                   roots.query_email AS query_email,
                   categories.name AS target_tag_name
            FROM registration_claims
            JOIN redemption_cards ON redemption_cards.id = registration_claims.card_id
            JOIN access_mappings ON access_mappings.id = registration_claims.mapping_id
            JOIN access_mappings roots ON roots.id = registration_claims.root_mapping_id
            JOIN categories ON categories.id = registration_claims.target_tag_id
        """

    @staticmethod
    def _normalize_card_code(code: str) -> str:
        compact = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
        if len(compact) == 18 and compact.startswith("CM"):
            payload = compact[2:]
            return "CM-" + "-".join(payload[index : index + 4] for index in range(0, 16, 4))
        return (code or "").strip().upper()

    @staticmethod
    def _generate_card_code() -> str:
        alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
        payload = "".join(secrets.choice(alphabet) for _ in range(16))
        return "CM-" + "-".join(payload[index : index + 4] for index in range(0, 16, 4))

    @staticmethod
    def _normalize_expiry(value: str, timezone_name: str = "UTC") -> str:
        normalized = (value or "").strip()
        if not normalized:
            return ""
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid expiry") from exc
        if len(normalized) == 10:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        if parsed.tzinfo is None:
            try:
                parsed = parsed.replace(tzinfo=ZoneInfo((timezone_name or "UTC").strip() or "UTC"))
            except ZoneInfoNotFoundError as exc:
                raise ValueError("invalid expiry timezone") from exc
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _split_rule_lines(values: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
        candidates = values.splitlines() if isinstance(values, str) else list(values)
        return tuple(str(value).strip() for value in candidates if str(value).strip())

    @staticmethod
    def _normalize_extraction_mode(value: str) -> str:
        normalized = (value or "rules").strip().lower()
        if normalized not in VALID_EXTRACTION_MODES:
            raise ValueError("verification extraction mode is invalid")
        return normalized

    @staticmethod
    def _normalize_rule_values(
        values: str | list[str] | tuple[str, ...],
    ) -> tuple[str, ...]:
        if isinstance(values, str):
            candidates = re.split(r"[\r\n,]+", values)
        else:
            candidates = list(values)
        normalized: list[str] = []
        seen: set[str] = set()
        for value in candidates:
            item = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
            if not item or item in seen:
                continue
            if len(item) > 200:
                raise ValueError("mail matching rule is too long")
            normalized.append(item)
            seen.add(item)
        if len(normalized) > 50:
            raise ValueError("too many mail matching rules")
        return tuple(normalized)

    @staticmethod
    def _decode_rule_values(value: object) -> tuple[str, ...]:
        raw = str(value or "").strip()
        if not raw:
            return ()
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = re.split(r"[\r\n,]+", raw)
        if not isinstance(parsed, list):
            return ()
        return tuple(str(item).strip() for item in parsed if str(item).strip())

    @staticmethod
    def _normalize_tag_ids(values: list[int] | tuple[int, ...] | None) -> tuple[int, ...]:
        if not values:
            return ()
        normalized = sorted({int(value) for value in values if int(value) > 0})
        if len(normalized) > 100:
            raise ValueError("too many tag filters")
        return tuple(normalized)

    @staticmethod
    def _decode_int_values(value: object) -> tuple[int, ...]:
        raw = str(value or "").strip()
        if not raw:
            return ()
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        if not isinstance(parsed, list):
            return ()
        result: list[int] = []
        for item in parsed:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in result:
                result.append(number)
        return tuple(sorted(result))

    def _insert_verification_event(
        self,
        connection: sqlite3.Connection,
        *,
        root_mapping_id: int,
        mapping_id: int,
        tag_id: int,
        source: str,
        recipient_email: str,
        address_mode: str,
        occurred_at: str,
        claim_id: int = 0,
        email_id: int = 0,
        fallback_event_key: str = "",
    ) -> bool:
        """在调用方事务内幂等写入一条真实成功接码事件。"""

        normalized_source = source.strip().lower()
        if normalized_source not in VALID_VERIFICATION_EVENT_SOURCES:
            raise ValueError("invalid verification event source")
        normalized_address_mode = (address_mode or "primary").strip().lower()
        if normalized_address_mode not in VALID_ADDRESS_MODES:
            raise ValueError("invalid address mode")
        normalized_email_id = max(0, int(email_id))
        normalized_claim_id = max(0, int(claim_id))
        if normalized_email_id > 0:
            # CloudMail 邮件 ID 在同一主邮箱族内唯一。跨公开台、后台和 API
            # 统一使用同一个键，可避免重复轮询或重试把同一封邮件统计多次。
            event_key = (
                f"mail:{int(root_mapping_id)}:{int(tag_id)}:{normalized_email_id}"
            )
        else:
            event_key = fallback_event_key.strip()
            if not event_key and normalized_claim_id > 0:
                event_key = f"registration_claim:{normalized_claim_id}:completion"
        if not event_key:
            raise ValueError("verification event key is required")

        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO verification_events (
                event_key, root_mapping_id, mapping_id, tag_id, source, claim_id,
                email_id, recipient_email, address_mode, occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                int(root_mapping_id),
                int(mapping_id),
                int(tag_id),
                normalized_source,
                normalized_claim_id or None,
                normalized_email_id,
                recipient_email.strip().lower(),
                normalized_address_mode,
                occurred_at,
            ),
        )
        return cursor.rowcount == 1

    def _insert_icloud_alias(
        self,
        connection: sqlite3.Connection,
        parent: sqlite3.Row,
        *,
        alias_tag: str | None,
        target_site: str,
    ) -> int:
        parent_email = str(parent["recipient_email"]).strip().lower()
        if "@" not in parent_email:
            raise ValueError("invalid parent email")
        local_part, domain = parent_email.rsplit("@", 1)
        if domain != "icloud.com" or "+" in local_part:
            raise ValueError("icloud alias requires a non-alias icloud.com address")

        target_tag = connection.execute(
            "SELECT id, alias_use_limit FROM categories WHERE normalized_name = ?",
            (self._category_key(target_site),),
        ).fetchone()
        if target_tag is not None and int(target_tag["alias_use_limit"] or 0) > 0:
            generated_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM alias_generation_events
                    WHERE root_mapping_id = ? AND tag_id = ?
                    """,
                    (int(parent["id"]), int(target_tag["id"])),
                ).fetchone()[0]
            )
            if generated_count >= int(target_tag["alias_use_limit"]):
                raise ValueError("alias use limit reached")

        requested_tag = "" if alias_tag is None else alias_tag.strip().lower()
        if requested_tag and not _ICLOUD_ALIAS_TAG_PATTERN.fullmatch(requested_tag):
            raise ValueError("invalid icloud alias tag")

        candidate_tag = requested_tag
        recipient_email = ""
        for _attempt in range(32):
            candidate_tag = candidate_tag or secrets.token_hex(5)
            recipient_email = f"{local_part}+{candidate_tag}@{domain}"
            duplicate = connection.execute(
                "SELECT 1 FROM access_mappings WHERE LOWER(recipient_email) = ? LIMIT 1",
                (recipient_email,),
            ).fetchone()
            if duplicate is None:
                break
            if requested_tag:
                raise ValueError("icloud alias already exists")
            candidate_tag = ""
        else:
            raise ValueError("unable to generate unique icloud alias")

        created_at = self._now()
        for _attempt in range(8):
            access_key = self._generate_key()
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO access_mappings (
                        recipient_email, query_email, access_key, label, category, created_at,
                        status, claimed_at, claimed_by, used_at, last_seen_email_id, target_site,
                        address_kind, parent_mapping_id, alias_tag, reuse_policy, first_used_at
                    )
                    VALUES (?, ?, ?, '', ?, ?, 'idle', '', '', '', ?, ?,
                            'icloud_alias', ?, ?, 'reusable', '')
                    """,
                    (
                        recipient_email,
                        str(parent["query_email"]),
                        access_key,
                        str(parent["category"] or ""),
                        created_at,
                        int(parent["last_seen_email_id"] or 0),
                        target_site.strip(),
                        int(parent["id"]),
                        candidate_tag,
                    ),
                )
                mapping_id = int(cursor.lastrowid)
                if target_tag is not None:
                    connection.execute(
                        """
                        INSERT INTO alias_generation_events (
                            mapping_id, root_mapping_id, tag_id, created_at
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            mapping_id,
                            int(parent["id"]),
                            int(target_tag["id"]),
                            created_at,
                        ),
                    )
                return mapping_id
            except sqlite3.IntegrityError:
                continue
        raise ValueError("unable to generate unique access key")

    def _release_claim_mapping(
        self,
        connection: sqlite3.Connection,
        claim: sqlite3.Row,
        *,
        retire_alias: bool,
    ) -> None:
        root_id = int(claim["root_mapping_id"])
        mapping_id = int(claim["mapping_id"])
        connection.execute(
            """
            UPDATE access_mappings
            SET status = 'idle', claimed_at = '', claimed_by = '', target_site = ''
            WHERE id = ?
            """,
            (root_id,),
        )
        if mapping_id != root_id:
            if retire_alias:
                connection.execute(
                    """
                    UPDATE access_mappings
                    SET status = 'idle', claimed_at = '', claimed_by = '',
                        reuse_policy = 'retired', access_key = ?
                    WHERE id = ?
                    """,
                    (self._generate_key(), mapping_id),
                )
            else:
                connection.execute(
                    "UPDATE access_mappings SET status = 'idle', claimed_at = '', claimed_by = '' WHERE id = ?",
                    (mapping_id,),
                )

    def _register_card_skip(
        self,
        connection: sqlite3.Connection,
        card_id: int,
        now: str,
        *,
        skip_limit: int,
        cooldown_minutes: int,
    ) -> None:
        row = connection.execute(
            "SELECT consecutive_skips FROM redemption_cards WHERE id = ?",
            (int(card_id),),
        ).fetchone()
        if row is None:
            raise ValueError("card not found")
        next_skips = int(row["consecutive_skips"] or 0) + 1
        if next_skips >= skip_limit:
            cooldown_until = (
                datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                + timedelta(minutes=cooldown_minutes)
            ).strftime("%Y-%m-%d %H:%M:%S")
            next_skips = 0
        else:
            cooldown_until = ""
        connection.execute(
            """
            UPDATE redemption_cards
            SET consecutive_skips = ?, cooldown_until = ?, updated_at = ?
            WHERE id = ?
            """,
            (next_skips, cooldown_until, now, int(card_id)),
        )

    def _expire_pending_claims(
        self,
        connection: sqlite3.Connection,
        *,
        now: str,
        timeout_minutes: int,
    ) -> int:
        cutoff = (
            datetime.strptime(now, "%Y-%m-%d %H:%M:%S") - timedelta(minutes=int(timeout_minutes))
        ).strftime("%Y-%m-%d %H:%M:%S")
        rows = connection.execute(
            """
            SELECT * FROM registration_claims
            WHERE status = 'pending' AND created_at <= ?
            ORDER BY id ASC
            """,
            (cutoff,),
        ).fetchall()
        for claim in rows:
            connection.execute(
                "UPDATE registration_claims SET status = 'timed_out', ended_at = ? WHERE id = ?",
                (now, int(claim["id"])),
            )
            self._release_claim_mapping(connection, claim, retire_alias=True)
        return len(rows)

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
                    target_site TEXT NOT NULL DEFAULT '',
                    address_kind TEXT NOT NULL DEFAULT 'primary',
                    parent_mapping_id INTEGER NOT NULL DEFAULT 0,
                    alias_tag TEXT NOT NULL DEFAULT '',
                    reuse_policy TEXT NOT NULL DEFAULT 'reusable',
                    first_used_at TEXT NOT NULL DEFAULT '',
                    claim_baseline_ready INTEGER NOT NULL DEFAULT 1,
                    claim_source_tag_id INTEGER NOT NULL DEFAULT 0
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    color TEXT NOT NULL DEFAULT '',
                    archived INTEGER NOT NULL DEFAULT 0,
                    kind TEXT NOT NULL DEFAULT 'business',
                    sender_patterns TEXT NOT NULL DEFAULT '[]',
                    subject_keywords TEXT NOT NULL DEFAULT '[]',
                    prevents_reuse INTEGER NOT NULL DEFAULT 0,
                    alias_use_limit INTEGER NOT NULL DEFAULT 0,
                    code_patterns TEXT NOT NULL DEFAULT '[]',
                    extraction_mode TEXT NOT NULL DEFAULT 'rules'
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
            if "address_kind" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN address_kind TEXT NOT NULL DEFAULT 'primary'"
                )
            if "parent_mapping_id" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN parent_mapping_id INTEGER NOT NULL DEFAULT 0"
                )
            if "alias_tag" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN alias_tag TEXT NOT NULL DEFAULT ''"
                )
            if "reuse_policy" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN reuse_policy TEXT NOT NULL DEFAULT 'reusable'"
                )
            if "first_used_at" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN first_used_at TEXT NOT NULL DEFAULT ''"
                )
            if "claim_baseline_ready" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN claim_baseline_ready INTEGER NOT NULL DEFAULT 1"
                )
                # 升级前仍在轮询的领取继续沿用原有时间/邮件水位，避免把已经到达
                # 的有效验证码在升级瞬间吞掉；升级后的新领取会显式写入未就绪状态。
            if "claim_source_tag_id" not in columns:
                connection.execute(
                    "ALTER TABLE access_mappings ADD COLUMN claim_source_tag_id INTEGER NOT NULL DEFAULT 0"
                )
            category_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(categories)").fetchall()
            }
            if "color" not in category_columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN color TEXT NOT NULL DEFAULT ''"
                )
            if "archived" not in category_columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
                )
            if "kind" not in category_columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN kind TEXT NOT NULL DEFAULT 'business'"
                )
            if "sender_patterns" not in category_columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN sender_patterns TEXT NOT NULL DEFAULT '[]'"
                )
            if "subject_keywords" not in category_columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN subject_keywords TEXT NOT NULL DEFAULT '[]'"
                )
            if "prevents_reuse" not in category_columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN prevents_reuse INTEGER NOT NULL DEFAULT 0"
                )
            if "alias_use_limit" not in category_columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN alias_use_limit INTEGER NOT NULL DEFAULT 0"
                )
            if "code_patterns" not in category_columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN code_patterns TEXT NOT NULL DEFAULT '[]'"
                )
            if "extraction_mode" not in category_columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN extraction_mode TEXT NOT NULL DEFAULT 'rules'"
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
            active_claim_rows = connection.execute(
                """
                SELECT id, claimed_by
                FROM access_mappings
                WHERE status = 'in_progress' AND claimed_by != ''
                ORDER BY claimed_by ASC, claimed_at DESC, id ASC
                """
            ).fetchall()
            seen_claim_owners: set[str] = set()
            for row in active_claim_rows:
                claim_owner = str(row["claimed_by"])
                if claim_owner not in seen_claim_owners:
                    seen_claim_owners.add(claim_owner)
                    continue
                connection.execute(
                    """
                    UPDATE access_mappings
                    SET status = 'idle', claimed_at = '', claimed_by = '', target_site = ''
                    WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_access_mappings_one_active_claim_per_owner
                ON access_mappings (claimed_by)
                WHERE status = 'in_progress' AND claimed_by != ''
                """
            )
            self._sync_categories(connection)
            self._initialize_registration_tables(connection)
            self._backfill_mapping_tags(connection)
            connection.commit()

    def _initialize_registration_tables(self, connection: sqlite3.Connection) -> None:
        """创建注册台扩展表。

        旧版 ``category`` 字段继续保留，新的业务语义统一写入多对多标签表，
        从而让旧 API 在升级期间仍可正常工作。
        """
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS mapping_tags (
                mapping_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL,
                PRIMARY KEY (mapping_id, tag_id),
                FOREIGN KEY (mapping_id) REFERENCES access_mappings(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES categories(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_mapping_tags_tag_id
            ON mapping_tags (tag_id, mapping_id);

            CREATE TABLE IF NOT EXISTS alias_generation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mapping_id INTEGER NOT NULL UNIQUE,
                root_mapping_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                -- 裂变地址本身允许作为临时数据清理，但生成次数属于长期限额流水，
                -- 因此 mapping_id 仅作审计快照，不对临时地址建立级联外键。
                FOREIGN KEY (root_mapping_id) REFERENCES access_mappings(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES categories(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_alias_generation_events_root_tag
            ON alias_generation_events (root_mapping_id, tag_id, id);

            CREATE TABLE IF NOT EXISTS card_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS card_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                target_tag_id INTEGER NOT NULL,
                delivery_mode TEXT NOT NULL,
                address_mode TEXT NOT NULL,
                uses_per_card INTEGER NOT NULL,
                source_scope TEXT NOT NULL DEFAULT 'all_reusable',
                include_tag_ids TEXT NOT NULL DEFAULT '[]',
                exclude_tag_ids TEXT NOT NULL DEFAULT '[]',
                expires_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (category_id) REFERENCES card_categories(id) ON DELETE RESTRICT,
                FOREIGN KEY (target_tag_id) REFERENCES categories(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS redemption_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                code TEXT NOT NULL UNIQUE,
                total_uses INTEGER NOT NULL,
                remaining_uses INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                consecutive_skips INTEGER NOT NULL DEFAULT 0,
                cooldown_until TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (batch_id) REFERENCES card_batches(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_redemption_cards_batch
            ON redemption_cards (batch_id, id);

            CREATE TABLE IF NOT EXISTS registration_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                mapping_id INTEGER NOT NULL,
                root_mapping_id INTEGER NOT NULL,
                target_tag_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                address_mode TEXT NOT NULL,
                verification_code TEXT NOT NULL DEFAULT '',
                email_id INTEGER NOT NULL DEFAULT 0,
                baseline_email_id INTEGER NOT NULL DEFAULT 0,
                baseline_ready INTEGER NOT NULL DEFAULT 1,
                view_token TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                ended_at TEXT NOT NULL DEFAULT '',
                revoked_at TEXT NOT NULL DEFAULT '',
                superseded_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (card_id) REFERENCES redemption_cards(id) ON DELETE RESTRICT,
                FOREIGN KEY (mapping_id) REFERENCES access_mappings(id) ON DELETE RESTRICT,
                FOREIGN KEY (root_mapping_id) REFERENCES access_mappings(id) ON DELETE RESTRICT,
                FOREIGN KEY (target_tag_id) REFERENCES categories(id) ON DELETE RESTRICT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_registration_claims_one_pending_per_card
            ON registration_claims (card_id)
            WHERE status = 'pending';

            CREATE UNIQUE INDEX IF NOT EXISTS idx_registration_claims_one_pending_per_family
            ON registration_claims (root_mapping_id)
            WHERE status = 'pending';

            CREATE INDEX IF NOT EXISTS idx_registration_claims_card_history
            ON registration_claims (card_id, id DESC);

            CREATE INDEX IF NOT EXISTS idx_registration_claims_family_history
            ON registration_claims (root_mapping_id, id DESC);

            CREATE TABLE IF NOT EXISTS verification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                root_mapping_id INTEGER NOT NULL,
                mapping_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                claim_id INTEGER,
                email_id INTEGER NOT NULL DEFAULT 0,
                recipient_email TEXT NOT NULL DEFAULT '',
                address_mode TEXT NOT NULL DEFAULT 'primary',
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (root_mapping_id) REFERENCES access_mappings(id) ON DELETE RESTRICT,
                FOREIGN KEY (mapping_id) REFERENCES access_mappings(id) ON DELETE RESTRICT,
                FOREIGN KEY (tag_id) REFERENCES categories(id) ON DELETE RESTRICT,
                FOREIGN KEY (claim_id) REFERENCES registration_claims(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_verification_events_tag_time
            ON verification_events (tag_id, occurred_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_verification_events_root_time
            ON verification_events (root_mapping_id, occurred_at DESC, id DESC);
            """
        )

        batch_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(card_batches)").fetchall()
        }
        if "source_scope" not in batch_columns:
            connection.execute(
                "ALTER TABLE card_batches ADD COLUMN source_scope TEXT NOT NULL DEFAULT 'all_reusable'"
            )
        if "include_tag_ids" not in batch_columns:
            connection.execute(
                "ALTER TABLE card_batches ADD COLUMN include_tag_ids TEXT NOT NULL DEFAULT '[]'"
            )
        if "exclude_tag_ids" not in batch_columns:
            connection.execute(
                "ALTER TABLE card_batches ADD COLUMN exclude_tag_ids TEXT NOT NULL DEFAULT '[]'"
            )

        claim_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(registration_claims)").fetchall()
        }
        if "baseline_email_id" not in claim_columns:
            connection.execute(
                "ALTER TABLE registration_claims ADD COLUMN baseline_email_id INTEGER NOT NULL DEFAULT 0"
            )
        if "baseline_ready" not in claim_columns:
            connection.execute(
                "ALTER TABLE registration_claims ADD COLUMN baseline_ready INTEGER NOT NULL DEFAULT 1"
            )
            # 历史领取保留原有边界，避免升级时漏掉已到达的验证码；新领取由路由
            # 显式进入未就绪状态，并在邮箱交付前完成完整快照。
        if "view_token" not in claim_columns:
            connection.execute(
                "ALTER TABLE registration_claims ADD COLUMN view_token TEXT NOT NULL DEFAULT ''"
            )
        if "revoked_at" not in claim_columns:
            connection.execute(
                "ALTER TABLE registration_claims ADD COLUMN revoked_at TEXT NOT NULL DEFAULT ''"
            )
        if "superseded_at" not in claim_columns:
            connection.execute(
                "ALTER TABLE registration_claims ADD COLUMN superseded_at TEXT NOT NULL DEFAULT ''"
            )
        empty_token_rows = connection.execute(
            "SELECT id FROM registration_claims WHERE view_token = '' ORDER BY id ASC"
        ).fetchall()
        for row in empty_token_rows:
            connection.execute(
                "UPDATE registration_claims SET view_token = ? WHERE id = ?",
                (secrets.token_urlsafe(24), int(row["id"])),
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_registration_claims_view_token ON registration_claims(view_token)"
        )
        # 公开注册领取保存了稳定的标签 ID，可完整回填历史；后台旧记录若已经
        # 清空目标平台则无法可靠推断，宁可从升级后开始计数，也不误伤其他平台。
        connection.execute(
            """
            INSERT OR IGNORE INTO alias_generation_events (
                mapping_id, root_mapping_id, tag_id, created_at
            )
            SELECT mapping_id, root_mapping_id, target_tag_id, created_at
            FROM registration_claims
            WHERE address_mode = 'icloud_alias'
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO alias_generation_events (
                mapping_id, root_mapping_id, tag_id, created_at
            )
            SELECT aliases.id, aliases.parent_mapping_id, categories.id, aliases.created_at
            FROM access_mappings aliases
            JOIN categories ON categories.name = aliases.target_site
            WHERE aliases.address_kind = 'icloud_alias'
              AND aliases.parent_mapping_id > 0
            """
        )
        # 旧数据中只有已完成的公开注册领取能被确定为一次真实成功接码；
        # 后台历史标签无法区分人工标注与成功接码，因此不做推测性回填。
        connection.execute(
            """
            INSERT OR IGNORE INTO verification_events (
                event_key, root_mapping_id, mapping_id, tag_id, source, claim_id,
                email_id, recipient_email, address_mode, occurred_at
            )
            SELECT
                CASE
                    WHEN registration_claims.email_id > 0 THEN
                        'mail:' || registration_claims.root_mapping_id || ':' ||
                        registration_claims.target_tag_id || ':' || registration_claims.email_id
                    ELSE 'registration_claim:' || registration_claims.id || ':completion'
                END,
                registration_claims.root_mapping_id,
                registration_claims.mapping_id,
                registration_claims.target_tag_id,
                'public_card',
                registration_claims.id,
                registration_claims.email_id,
                access_mappings.recipient_email,
                registration_claims.address_mode,
                CASE
                    WHEN registration_claims.completed_at != '' THEN registration_claims.completed_at
                    WHEN registration_claims.ended_at != '' THEN registration_claims.ended_at
                    ELSE registration_claims.created_at
                END
            FROM registration_claims
            JOIN access_mappings ON access_mappings.id = registration_claims.mapping_id
            WHERE registration_claims.status = 'completed'
            """
        )

    def _backfill_mapping_tags(self, connection: sqlite3.Connection) -> None:
        # SQLite 内置 LOWER 只可靠处理 ASCII。这里用与标签创建相同的
        # NFKC + casefold 规则回填，避免 Ä/全角字符等旧分类迁移后丢标签。
        category_ids = {
            str(row["normalized_name"]): int(row["id"])
            for row in connection.execute(
                "SELECT id, normalized_name FROM categories"
            ).fetchall()
        }
        legacy_rows = connection.execute(
            """
            SELECT id, category, created_at
            FROM access_mappings
            """
        ).fetchall()
        backfill_rows: list[tuple[int, int, str, str]] = []
        for row in legacy_rows:
            mapping_id = int(row["id"])
            category_key = self._category_key(str(row["category"]))
            desired_tag_id = category_ids.get(category_key)
            if desired_tag_id is None:
                connection.execute(
                    "DELETE FROM mapping_tags WHERE mapping_id = ? AND source = 'legacy_category'",
                    (mapping_id,),
                )
                continue
            connection.execute(
                """
                DELETE FROM mapping_tags
                WHERE mapping_id = ? AND source = 'legacy_category' AND tag_id != ?
                """,
                (mapping_id, desired_tag_id),
            )
            backfill_rows.append(
                (mapping_id, desired_tag_id, "legacy_category", str(row["created_at"]))
            )
        if backfill_rows:
            connection.executemany(
                """
                INSERT OR IGNORE INTO mapping_tags (mapping_id, tag_id, source, created_at)
                VALUES (?, ?, ?, ?)
                """,
                backfill_rows,
            )
        connection.execute(
            """
            UPDATE access_mappings
            SET first_used_at = used_at
            WHERE first_used_at = '' AND used_at != ''
            """
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
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
            search_clause += (
                " OR EXISTS (SELECT 1 FROM mapping_tags search_tags "
                "JOIN categories search_categories ON search_categories.id = search_tags.tag_id "
                "WHERE search_tags.mapping_id = access_mappings.id "
                "AND LOWER(search_categories.name) LIKE ?)"
            )
            clauses.append(f"({search_clause})")
            params.extend([wildcard] * 6)

        normalized_category = unicodedata.normalize("NFKC", category_filter or "").strip().casefold()
        if normalized_category:
            clauses.append(
                "EXISTS (SELECT 1 FROM mapping_tags filter_tags "
                "JOIN categories filter_categories ON filter_categories.id = filter_tags.tag_id "
                "WHERE filter_tags.mapping_id = access_mappings.id "
                "AND filter_categories.normalized_name = ?)"
            )
            params.append(normalized_category)

        return " AND ".join(clauses), params

    @staticmethod
    def _category_key(value: str) -> str:
        return unicodedata.normalize("NFKC", value or "").strip().casefold()

    def canonicalize_category(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value or "").strip()
        category_key = self._category_key(normalized)
        if not category_key:
            return ""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT name FROM categories WHERE normalized_name = ?",
                (category_key,),
            ).fetchone()
            if row is not None:
                return str(row["name"])

            connection.execute(
                """
                INSERT OR IGNORE INTO categories (name, normalized_name, kind, created_at)
                VALUES (?, ?, 'business', ?)
                """,
                (normalized, category_key, self._now()),
            )
            row = connection.execute(
                "SELECT name FROM categories WHERE normalized_name = ?",
                (category_key,),
            ).fetchone()
            connection.commit()

        return normalized if row is None else str(row["name"])

    def _sync_categories(self, connection: sqlite3.Connection) -> None:
        mapping_rows = connection.execute(
            """
            SELECT id, category
            FROM access_mappings
            WHERE TRIM(category) != ''
            ORDER BY id ASC
            """
        ).fetchall()
        existing_rows = connection.execute(
            "SELECT id, name, normalized_name FROM categories ORDER BY id ASC"
        ).fetchall()
        canonical_by_key = {
            str(row["normalized_name"]): str(row["name"])
            for row in existing_rows
        }

        statistics: dict[str, dict[str, tuple[int, int]]] = {}
        mapping_keys: dict[int, str] = {}
        for row in mapping_rows:
            mapping_id = int(row["id"])
            category = unicodedata.normalize("NFKC", str(row["category"] or "")).strip()
            category_key = self._category_key(category)
            if not category_key:
                continue
            mapping_keys[mapping_id] = category_key
            category_statistics = statistics.setdefault(category_key, {})
            count, first_id = category_statistics.get(category, (0, mapping_id))
            category_statistics[category] = (count + 1, min(first_id, mapping_id))

        for category_key, spellings in statistics.items():
            canonical_name = min(
                spellings,
                key=lambda name: (-spellings[name][0], spellings[name][1], name),
            )
            if category_key in canonical_by_key:
                connection.execute(
                    "UPDATE categories SET name = ? WHERE normalized_name = ?",
                    (canonical_name, category_key),
                )
            else:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO categories (name, normalized_name, kind, created_at)
                    VALUES (?, ?, 'business', ?)
                    """,
                    (canonical_name, category_key, self._now()),
                )
            row = connection.execute(
                "SELECT name FROM categories WHERE normalized_name = ?",
                (category_key,),
            ).fetchone()
            canonical_by_key[category_key] = canonical_name if row is None else str(row["name"])

        for row in mapping_rows:
            mapping_id = int(row["id"])
            category_key = mapping_keys.get(mapping_id)
            if not category_key:
                continue
            canonical_name = canonical_by_key[category_key]
            if str(row["category"]) != canonical_name:
                connection.execute(
                    "UPDATE access_mappings SET category = ? WHERE id = ?",
                    (canonical_name, mapping_id),
                )

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
    def _normalize_alias_use_limit(value: int | str | None) -> int | None:
        if value is None:
            return None
        try:
            normalized = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("alias_use_limit must be a non-negative integer") from exc
        if normalized < 0:
            raise ValueError("alias_use_limit must be a non-negative integer")
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
        keys = set(row.keys())
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
            address_kind=(row["address_kind"] if "address_kind" in keys else "primary") or "primary",
            parent_mapping_id=int(row["parent_mapping_id"] or 0) if "parent_mapping_id" in keys else 0,
            alias_tag=(row["alias_tag"] if "alias_tag" in keys else "") or "",
            reuse_policy=(row["reuse_policy"] if "reuse_policy" in keys else "reusable") or "reusable",
            first_used_at=(row["first_used_at"] if "first_used_at" in keys else "") or "",
            claim_source_tag_id=(
                int(row["claim_source_tag_id"] or 0) if "claim_source_tag_id" in keys else 0
            ),
        )
