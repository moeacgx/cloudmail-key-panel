import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

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


def test_deleting_primary_mapping_also_deletes_unclaimed_icloud_aliases(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    root = store.create_mapping("family@icloud.com")
    alias = store.create_icloud_alias(root.id, alias_tag="temporary")

    store.delete_mapping(root.id)

    assert store.get_by_id(root.id) is None
    assert store.get_by_id(alias.id) is None


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


def test_key_store_reuses_category_spelling_and_hides_legacy_case_variants(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping(recipient_email="legacy@example.com", category="GPT废号")
    lower_one = store.create_mapping(recipient_email="lower-one@example.com", category="temporary-one")
    lower_two = store.create_mapping(recipient_email="lower-two@example.com", category="temporary-two")

    with sqlite3.connect(store.db_path) as connection:
        connection.executemany(
            "UPDATE access_mappings SET category = ? WHERE id = ?",
            [("gpt废号", lower_one.id), ("gpt废号", lower_two.id)],
        )
        connection.commit()

    store = KeyStore(store.db_path)
    canonical = store.create_mapping(recipient_email="fullwidth@example.com", category="ＧＰＴ废号")

    assert canonical.category == "gpt废号"
    assert store.list_categories() == ["gpt废号"]
    assert [(item.name, item.count) for item in store.list_category_options()] == [
        ("gpt废号", 4),
        ("temporary-one", 0),
        ("temporary-two", 0),
    ]
    assert len(store.list_mappings(category_filter="GPT废号")) == 4


def test_key_store_backfills_stable_category_ids_and_keeps_empty_categories(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    original = KeyStore(db_path)
    first = original.create_mapping(recipient_email="first@example.com", category="Queue")
    second = original.create_mapping(recipient_email="second@example.com", category="temporary")

    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE access_mappings SET category = 'queue' WHERE id = ?", (second.id,))
        connection.execute("DROP TABLE categories")
        connection.commit()

    migrated = KeyStore(db_path)
    category_id = migrated.get_category_id("ＱＵＥＵＥ")
    assert category_id is not None
    assert migrated.get_category_name(category_id) == "Queue"
    assert [(item.id, item.name, item.count) for item in migrated.list_category_options()] == [
        (category_id, "Queue", 2)
    ]
    assert {mapping.category for mapping in migrated.list_mappings()} == {"Queue"}

    restarted = KeyStore(db_path)
    assert restarted.get_category_id("queue") == category_id

    restarted.delete_mappings([first.id, second.id])
    assert restarted.count_mappings() == 0
    assert restarted.get_category_name(category_id) == "Queue"
    assert [(item.id, item.name, item.count) for item in restarted.list_category_options()] == [
        (category_id, "Queue", 0)
    ]

    restarted.create_mapping(recipient_email="third@example.com", category="Ｑｕｅｕｅ")
    assert restarted.get_category_id("queue") == category_id
    assert [(item.id, item.name, item.count) for item in restarted.list_category_options()] == [
        (category_id, "Queue", 1)
    ]



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


def test_key_store_persists_verification_ai_and_tag_extraction_rules(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    saved = store.save_verification_extraction_settings(
        mode="fallback",
        custom_patterns=r"token=([0-9]{4,8})" + "\n" + r"order=([A-Z0-9-]+)",
        base_url=" https://ai.example.com/v1/ ",
        api_key=" secret-key ",
        model=" extract-model ",
        timeout_seconds="8",
    )
    tag = store.create_tag(
        "SpaceXAI",
        kind="service",
        code_patterns=r"\b(?P<code>[A-Z0-9]{3}-[A-Z0-9]{3})\b" + "\n" + r"order=(\d{8})",
        extraction_mode="ai_fallback",
    )
    restarted = KeyStore(tmp_path / "app.db")
    loaded = restarted.get_verification_extraction_settings()
    loaded_tag = restarted.get_tag(tag.id)

    assert saved.base_url == "https://ai.example.com/v1"
    assert saved.mode == "fallback"
    assert saved.custom_patterns == (
        r"token=([0-9]{4,8})",
        r"order=([A-Z0-9-]+)",
    )
    assert loaded.mode == "fallback"
    assert loaded.custom_patterns == saved.custom_patterns
    assert loaded.api_key == "secret-key"
    assert loaded.model == "extract-model"
    assert loaded.timeout_seconds == 8
    assert loaded_tag is not None
    assert loaded_tag.code_patterns == (
        r"\b(?P<code>[A-Z0-9]{3}-[A-Z0-9]{3})\b",
        r"order=(\d{8})",
    )
    assert loaded_tag.extraction_mode == "ai_fallback"


def test_verification_ai_blank_key_is_preserved_and_can_be_cleared(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.save_verification_extraction_settings(
        base_url="https://ai.example.com/v1",
        api_key="secret-key",
        model="extract-model",
    )

    preserved = store.save_verification_extraction_settings(
        base_url="https://ai.example.com/v2",
        api_key="",
        model="extract-model-v2",
    )
    assert preserved.api_key == "secret-key"

    with pytest.raises(ValueError, match="api key is required for changed origin"):
        store.save_verification_extraction_settings(
            base_url="https://other-ai.example.com/v1",
            api_key="",
            model="extract-model-v2",
        )
    unchanged = store.get_verification_extraction_settings()
    assert unchanged.base_url == "https://ai.example.com/v2"
    assert unchanged.api_key == "secret-key"

    cleared = store.save_verification_extraction_settings(
        mode="fallback",
        base_url="",
        api_key="",
        model="",
        clear_api_key=True,
    )
    assert cleared.api_key == ""
    assert cleared.mode == "off"


def test_tag_rejects_invalid_extraction_rule(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    with pytest.raises(ValueError, match="pattern is invalid"):
        store.create_tag("Broken", code_patterns="(")


def test_global_extraction_mode_requires_complete_ai_config(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    with pytest.raises(ValueError, match="verification ai config is required"):
        store.save_verification_extraction_settings(mode="only")

    with pytest.raises(ValueError, match="verification extraction global mode is invalid"):
        store.save_verification_extraction_settings(mode="sometimes")


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


def test_key_store_completion_adds_platform_tag_without_overwriting_source_category(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("ChatGPT", kind="service")
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
    chatgpt_tag_id = store.get_category_id("ChatGPT")
    assert chatgpt_tag_id is not None

    completed = store.complete_workbench_mapping(
        first.id,
        target_tag_id=chatgpt_tag_id,
        claimed_by=session_a,
        verification_source="admin_workbench",
        email_id=101,
    )
    next_claimed = store.claim_next_available_mapping(
        category_filter="ChatGPT",
        target_site="ChatGPT",
        claimed_by=session_a,
    )

    assert completed.status == "idle"
    assert completed.category == "ChatGPT"
    assert completed.claimed_by == ""
    assert completed.used_at
    assert next_claimed.id == second.id
    assert next_claimed.status == "in_progress"
    assert next_claimed.claimed_by == session_a
    with pytest.raises(ValueError, match="mapping not claimed by this session"):
        store.complete_workbench_mapping(
            second.id,
            target_tag_id=chatgpt_tag_id,
            claimed_by=session_b,
            verification_source="admin_workbench",
            email_id=102,
        )

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

    assert reused.id == used_elsewhere.id
    assert reused.used_at == ""
    assert store.claim_next_available_mapping(category_filter="已使用", claimed_by=session_c) is None


def test_key_store_claims_only_later_mappings_after_completion(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    first = store.create_mapping(recipient_email="first@example.com", category="unused")
    second = store.create_mapping(recipient_email="second@example.com", category="unused")
    session_id = "session-a"

    claimed = store.claim_next_available_mapping(claimed_by=session_id)
    assert claimed is not None
    assert claimed.id == first.id
    used_tag = store.create_tag("used")

    store.complete_workbench_mapping(
        first.id,
        target_tag_id=used_tag.id,
        claimed_by=session_id,
        verification_source="admin_workbench",
        email_id=201,
    )
    next_mapping = store.claim_next_available_mapping(
        claimed_by=session_id,
        after_mapping_id=first.id,
    )

    assert next_mapping is not None
    assert next_mapping.id == second.id

    store.complete_workbench_mapping(
        second.id,
        target_tag_id=used_tag.id,
        claimed_by=session_id,
        verification_source="admin_workbench",
        email_id=202,
    )
    assert store.claim_next_available_mapping(
        claimed_by=session_id,
        after_mapping_id=second.id,
    ) is None


def test_key_store_same_client_concurrent_claim_is_atomic(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping(recipient_email="first@example.com", category="unused")
    store.create_mapping(recipient_email="second@example.com", category="unused")
    barrier = Barrier(2)

    def claim() -> int:
        barrier.wait(timeout=5)
        mapping = store.claim_next_available_mapping(category_filter="unused", claimed_by="api:same-worker")
        assert mapping is not None
        return mapping.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed_ids = list(executor.map(lambda _index: claim(), range(2)))

    assert claimed_ids[0] == claimed_ids[1]
    active = [mapping for mapping in store.list_mappings() if mapping.status == "in_progress"]
    assert len(active) == 1
    assert active[0].claimed_by == "api:same-worker"


def test_key_store_owned_reset_cannot_release_a_new_owner_claim(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping(recipient_email="first@example.com", category="unused")
    store.claim_next_available_mapping(category_filter="unused", claimed_by="api:old-worker")

    store.reset_mapping_status(mapping.id)
    reclaimed = store.claim_next_available_mapping(category_filter="unused", claimed_by="api:new-worker")
    assert reclaimed is not None
    assert reclaimed.id == mapping.id

    with pytest.raises(ValueError, match="mapping not claimed by this session"):
        store.reset_mapping_status(mapping.id, claimed_by="api:old-worker")

    still_claimed = store.get_by_id(mapping.id)
    assert still_claimed is not None
    assert still_claimed.status == "in_progress"
    assert still_claimed.claimed_by == "api:new-worker"


def test_workbench_snapshot_finalization_is_idempotent(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping("snapshot-idempotent@icloud.com")
    claimed = store.claim_next_available_mapping(
        claimed_by="api:snapshot-worker",
        defer_email_baseline=True,
    )

    assert claimed is not None and claimed.id == mapping.id
    assert not store.is_workbench_claim_baseline_ready(
        mapping.id,
        claimed_by="api:snapshot-worker",
    )

    store.finalize_workbench_claim_baseline(
        mapping.id,
        claimed_by="api:snapshot-worker",
        baseline_email_id=100,
    )
    store.finalize_workbench_claim_baseline(
        mapping.id,
        claimed_by="api:snapshot-worker",
        baseline_email_id=200,
    )

    finalized = store.get_by_id(mapping.id)
    assert finalized is not None
    assert finalized.last_seen_email_id == 100
    assert store.is_workbench_claim_baseline_ready(
        mapping.id,
        claimed_by="api:snapshot-worker",
    )


def test_migration_preserves_active_legacy_workbench_claim_boundary(tmp_path) -> None:
    database = tmp_path / "legacy-active-snapshot.db"
    store = KeyStore(database)
    mapping = store.create_mapping("legacy-active@icloud.com")
    claimed = store.claim_next_available_mapping(claimed_by="api:legacy-worker")

    assert claimed is not None and claimed.id == mapping.id
    assert store.is_workbench_claim_baseline_ready(
        mapping.id,
        claimed_by="api:legacy-worker",
    )

    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE access_mappings DROP COLUMN claim_baseline_ready")
        connection.commit()

    migrated = KeyStore(database)

    assert migrated.is_workbench_claim_baseline_ready(
        mapping.id,
        claimed_by="api:legacy-worker",
    )


def test_delete_tag_only_allows_completely_unused_tags(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    unused = store.create_tag("临时空标签", kind="business")
    used = store.create_tag("已有历史标签", kind="service")
    store.create_mapping("tagged@example.com", category=used.name)

    store.delete_tag(unused.id)

    assert store.get_tag(unused.id) is None
    with pytest.raises(ValueError, match="tag is in use"):
        store.delete_tag(used.id)
    assert store.get_tag(used.id) is not None


def test_delete_tag_rejects_system_tags(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    system_tag = store.ensure_independent_system_tag()

    with pytest.raises(ValueError, match="system tag cannot be deleted"):
        store.delete_tag(system_tag.id)
