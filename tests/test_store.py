import sqlite3

import pytest

from app.store import KeyStore


def test_key_store_persists_and_lists_mappings(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    created = store.create_mapping(
        recipient_email="Cranes_Solute.1O@icloud.com",
        query_email=" OpenAI@eve.ink ",
        access_key="demo-key-001",
        label="demo",
        category="OpenAI OTP",
    )

    loaded = store.get_by_key("demo-key-001")
    listed = store.list_mappings()

    assert created.recipient_email == "cranes_solute.1o@icloud.com"
    assert created.query_email == "openai@eve.ink"
    assert created.category == "OpenAI OTP"
    assert loaded is not None
    assert loaded.recipient_email == "cranes_solute.1o@icloud.com"
    assert loaded.query_email == "openai@eve.ink"
    assert loaded.category == "OpenAI OTP"
    assert listed[0].access_key == "demo-key-001"
    assert listed[0].label == "demo"
    assert listed[0].category == "OpenAI OTP"


def test_key_store_generates_key_when_not_provided(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    created = store.create_mapping(recipient_email="buyer@example.com")

    assert created.access_key
    assert len(created.access_key) >= 16
    assert store.get_by_key(created.access_key) is not None


def test_key_store_rejects_duplicate_access_keys(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping(recipient_email="buyer@example.com", access_key="repeat-key")

    with pytest.raises(ValueError):
        store.create_mapping(recipient_email="another@example.com", access_key="repeat-key")


def test_key_store_updates_and_deletes_mapping(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    created = store.create_mapping(
        recipient_email="buyer@example.com",
        query_email="openai@eve.ink",
        access_key="buyer-key",
        label="before",
    )

    updated = store.update_mapping(
        mapping_id=created.id,
        recipient_email="buyer2@example.com",
        query_email="mail@eve.ink",
        access_key="buyer-key-2",
        label="after",
    )

    assert updated.recipient_email == "buyer2@example.com"
    assert updated.query_email == "mail@eve.ink"
    assert updated.access_key == "buyer-key-2"
    assert updated.label == "after"
    assert store.get_by_key("buyer-key") is None
    assert store.get_by_key("buyer-key-2") is not None

    store.delete_mapping(updated.id)

    assert store.get_by_key("buyer-key-2") is None


def test_key_store_supports_search_pagination_category_filter_and_batch_delete(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    alpha_key = store.create_mapping(
        recipient_email="alpha@example.com",
        query_email="openai@eve.ink",
        access_key="alpha-key-1",
        label="starter",
        category="OpenAI",
    )
    beta_key = store.create_mapping(
        recipient_email="beta@example.com",
        query_email="openai@eve.ink",
        access_key="beta-key-1",
        label="alpha label",
        category="Apple",
    )
    gamma_key = store.create_mapping(
        recipient_email="gamma@example.com",
        query_email="alpha@eve.ink",
        access_key="gamma-key-1",
        label="normal",
        category="OpenAI",
    )
    store.create_mapping(
        recipient_email="delta@example.com",
        query_email="mail@eve.ink",
        access_key="delta-key-1",
        label="normal",
        category="",
    )

    page_one = store.list_mappings(search_query="alpha", limit=2, offset=0)
    page_two = store.list_mappings(search_query="alpha", limit=2, offset=2)
    openai_only = store.list_mappings(category_filter="openai")
    total = store.count_mappings(search_query="alpha")
    categories = store.list_categories()

    assert total == 3
    assert [item.access_key for item in page_one] == ["gamma-key-1", "beta-key-1"]
    assert [item.access_key for item in page_two] == ["alpha-key-1"]
    assert [item.access_key for item in openai_only] == ["gamma-key-1", "alpha-key-1"]
    assert categories == ["Apple", "OpenAI"]

    deleted = store.delete_mappings([alpha_key.id, gamma_key.id])

    assert deleted == 2
    assert store.get_by_key("alpha-key-1") is None
    assert store.get_by_key("gamma-key-1") is None
    assert store.get_by_key(beta_key.access_key) is not None



def test_key_store_persists_cloudmail_settings(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    saved = store.save_cloudmail_settings(
        base_url=" https://mail.boxmoe.eu.org/ ",
        api_token=" fixed-token-123 ",
        internal_admin_email=" Admin@Example.com ",
        internal_admin_password=" secret ",
        default_query_email=" OpenAI@eve.ink ",
        recent_email_limit=" 3 ",
        display_timezone=" Asia/Shanghai ",
    )
    loaded = store.get_cloudmail_settings(default_recent_email_limit=10)

    assert saved.base_url == "https://mail.boxmoe.eu.org/"
    assert saved.api_token == "fixed-token-123"
    assert saved.internal_admin_email == "admin@example.com"
    assert saved.internal_admin_password == "secret"
    assert saved.default_query_email == "openai@eve.ink"
    assert saved.recent_email_limit == 3
    assert saved.display_timezone == "Asia/Shanghai"
    assert loaded.base_url == "https://mail.boxmoe.eu.org/"
    assert loaded.api_token == "fixed-token-123"
    assert loaded.internal_admin_email == "admin@example.com"
    assert loaded.internal_admin_password == "secret"
    assert loaded.default_query_email == "openai@eve.ink"
    assert loaded.recent_email_limit == 3
    assert loaded.display_timezone == "Asia/Shanghai"


def test_key_store_keeps_legacy_categories_as_labels_and_initializes_status_idle(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE access_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_email TEXT NOT NULL,
                query_email TEXT NOT NULL DEFAULT '',
                access_key TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO access_mappings (recipient_email, query_email, access_key, label, category, created_at)
            VALUES (?, ?, ?, '', ?, '2026-04-18 10:00:00')
            """,
            [
                ("unused@example.com", "unused@example.com", "unused-key", "未使用"),
                ("used@example.com", "used@example.com", "used-key", "已使用"),
                ("progress@example.com", "progress@example.com", "progress-key", "注册中"),
                ("skipped@example.com", "skipped@example.com", "skipped-key", "跳过"),
                ("failed@example.com", "failed@example.com", "failed-key", "失败"),
            ],
        )
        connection.commit()

    store = KeyStore(db_path)

    assert store.get_by_key("unused-key").category == "未使用"
    assert store.get_by_key("used-key").category == "已使用"
    assert store.get_by_key("progress-key").category == "注册中"
    assert store.get_by_key("skipped-key").category == "跳过"
    assert store.get_by_key("failed-key").category == "失败"
    assert {store.get_by_key(key).status for key in ("unused-key", "used-key", "progress-key", "skipped-key", "failed-key")} == {
        "idle"
    }


def test_key_store_releases_legacy_unowned_in_progress_workbench_claims(tmp_path) -> None:
    db_path = tmp_path / "legacy-claims.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE access_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_email TEXT NOT NULL,
                query_email TEXT NOT NULL DEFAULT '',
                access_key TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'idle',
                claimed_at TEXT NOT NULL DEFAULT '',
                used_at TEXT NOT NULL DEFAULT '',
                last_seen_email_id INTEGER NOT NULL DEFAULT 0,
                target_site TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
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
                used_at,
                last_seen_email_id,
                target_site
            )
            VALUES (
                'claimed@example.com',
                'claimed@example.com',
                'claimed-key',
                '',
                'ChatGPT',
                '2026-04-18 10:00:00',
                'in_progress',
                '2026-04-18 10:01:00',
                '',
                0,
                'ChatGPT'
            )
            """
        )
        connection.commit()

    store = KeyStore(db_path)
    mapping = store.get_by_key("claimed-key")

    assert mapping.status == "idle"
    assert mapping.claimed_at == ""
    assert mapping.claimed_by == ""
    assert mapping.target_site == ""


def test_key_store_claims_by_category_and_completion_updates_category_not_terminal_status(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    first = store.create_mapping(
        recipient_email="first@example.com",
        access_key="first-key",
        category="ChatGPT",
    )
    second = store.create_mapping(
        recipient_email="second@example.com",
        access_key="second-key",
        category="ChatGPT",
    )
    used_elsewhere = store.create_mapping(
        recipient_email="used@example.com",
        access_key="used-key",
        category="已使用",
    )
    session_a = "session-a"
    session_b = "session-b"
    session_c = "session-c"

    claimed = store.claim_next_available_mapping(
        category_filter="ChatGPT",
        target_site="ChatGPT",
        claimed_by=session_a,
    )

    assert claimed.id == first.id
    assert claimed.status == "in_progress"
    assert claimed.claimed_at
    assert claimed.claimed_by == session_a
    assert claimed.target_site == "ChatGPT"
    assert store.get_current_workbench_mapping(category_filter="ChatGPT", claimed_by=session_a).id == first.id
    assert store.get_current_workbench_mapping(category_filter="ChatGPT", claimed_by=session_b) is None
    assert store.claim_next_available_mapping(category_filter="已使用", claimed_by=session_a).id == first.id

    completed = store.complete_workbench_mapping(
        first.id,
        category="已使用",
        target_site="ChatGPT",
        claimed_by=session_a,
    )
    next_claimed = store.claim_next_available_mapping(
        category_filter="ChatGPT",
        target_site="ChatGPT",
        claimed_by=session_a,
    )

    assert completed.status == "idle"
    assert completed.category == "已使用"
    assert completed.claimed_by == ""
    assert completed.used_at
    assert next_claimed.id == second.id
    assert next_claimed.status == "in_progress"
    assert next_claimed.claimed_by == session_a
    with pytest.raises(ValueError, match="mapping not claimed by this session"):
        store.complete_workbench_mapping(second.id, category="已使用", claimed_by=session_b)

    reset = store.reset_mapping_status(second.id)
    reclaimed = store.claim_next_available_mapping(
        category_filter="ChatGPT",
        target_site="ChatGPT",
        claimed_by=session_a,
    )

    assert reset.status == "idle"
    assert reset.category == "ChatGPT"
    assert reset.claimed_at == ""
    assert reset.claimed_by == ""
    assert reset.target_site == ""
    assert reclaimed.id == second.id

    reused = store.claim_next_available_mapping(category_filter="已使用", claimed_by=session_b)

    assert reused.id == first.id
    assert reused.used_at == ""
    assert store.claim_next_available_mapping(category_filter="已使用", claimed_by=session_c).id == used_elsewhere.id
