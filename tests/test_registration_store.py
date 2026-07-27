from __future__ import annotations

import sqlite3

import pytest

from app.store import KeyStore


def _create_batch(
    store: KeyStore,
    *,
    tag_id: int,
    delivery_mode: str = "custom",
    address_mode: str = "primary",
    uses: int = 3,
    source_scope: str = "all_reusable",
):
    category = store.create_card_category("零售卡")
    batch, cards = store.create_card_batch(
        name="测试批次",
        category_id=category.id,
        target_tag_id=tag_id,
        card_count=1,
        uses_per_card=uses,
        delivery_mode=delivery_mode,
        address_mode=address_mode,
        source_scope=source_scope,
    )
    return batch, cards[0]


def test_tags_are_additive_and_keep_platform_matching_rules(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping("reuse@icloud.com")
    gpt = store.create_tag(
        "GPT",
        "#123456",
        kind="service",
        sender_patterns="openai.com\n@tm.openai.com",
        subject_keywords="ChatGPT, verification code",
    )
    gemini = store.create_tag("Gemini", kind="service")

    store.add_mapping_tag(mapping.id, gpt.id, source="usage")
    store.add_mapping_tag(mapping.id, gemini.id, source="usage")

    refreshed = store.get_by_id(mapping.id)
    saved_gpt = store.get_tag(gpt.id)
    assert refreshed is not None
    assert set(refreshed.tags) == {"GPT", "Gemini"}
    assert saved_gpt is not None
    assert saved_gpt.sender_patterns == ("openai.com", "@tm.openai.com")
    assert saved_gpt.subject_keywords == ("chatgpt", "verification code")


def test_legacy_unicode_category_is_backfilled_as_tag(tmp_path) -> None:
    database = tmp_path / "app.db"
    store = KeyStore(database)
    mapping = store.create_mapping("unicode@example.com", category="Ä服务")
    with store._connect() as connection:
        connection.execute("DELETE FROM mapping_tags WHERE mapping_id = ?", (mapping.id,))
        connection.commit()

    migrated = KeyStore(database)
    assert [tag.name for tag in migrated.list_mapping_tags(mapping.id)] == ["Ä服务"]


def test_restarting_store_does_not_restore_a_manually_removed_legacy_tag(tmp_path) -> None:
    database = tmp_path / "app.db"
    store = KeyStore(database)
    old_tag = store.create_tag("旧库存", kind="business")
    new_tag = store.create_tag("新库存", kind="business")
    mapping = store.create_mapping("inventory@example.com", category=old_tag.name)

    store.set_mapping_tags(mapping.id, [new_tag.id])

    updated = store.get_by_id(mapping.id)
    assert updated is not None
    assert updated.category == "新库存"
    assert [tag.name for tag in store.list_mapping_tags(mapping.id)] == ["新库存"]

    restarted = KeyStore(database)
    assert [tag.name for tag in restarted.list_mapping_tags(mapping.id)] == ["新库存"]


def test_unused_system_tag_restores_stock_and_cannot_coexist_with_other_tags(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping("restore@example.com")
    unused = store.create_tag("未使用", kind="business")
    platform = store.create_tag("OpenAI", kind="service")
    inventory = store.create_tag("旧库存", kind="business")
    store.add_mapping_tag(mapping.id, platform.id, source="usage")
    store.add_mapping_tag(mapping.id, inventory.id, source="manual")

    store.set_mapping_tags(mapping.id, [unused.id])

    restored = store.get_by_id(mapping.id)
    assert unused.kind == "system"
    assert restored is not None and restored.category == "未使用"
    assert [tag.id for tag in store.list_mapping_tags(mapping.id)] == [unused.id]

    store.set_mapping_tags(mapping.id, [unused.id, platform.id])

    mixed = store.get_by_id(mapping.id)
    assert mixed is not None and mixed.category == "OpenAI"
    assert [tag.id for tag in store.list_mapping_tags(mapping.id)] == [platform.id]


def test_card_expiry_uses_configured_local_timezone(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    category = store.create_card_category("时区测试")
    target_tag = store.create_tag("独立邮箱", kind="system")

    batch, cards = store.create_card_batch(
        name="上海本地时间",
        category_id=category.id,
        target_tag_id=target_tag.id,
        card_count=1,
        uses_per_card=1,
        delivery_mode="independent",
        expires_at="2026-07-21T18:00",
        expiry_timezone="Asia/Shanghai",
    )

    assert batch.expires_at == "2026-07-21 10:00:00"
    assert cards[0].expires_at == "2026-07-21 10:00:00"


def test_primary_address_is_tagged_and_charged_only_after_success(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    first = store.create_mapping("first@icloud.com")
    second = store.create_mapping("second@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id, uses=2)

    pending = store.start_registration_claim(card.code, address_mode="primary")
    assert pending.mapping_id == first.id
    assert store.get_card_by_code(card.code).remaining_uses == 2
    assert store.list_mapping_tags(first.id) == []

    completed = store.complete_registration_claim(
        pending.id,
        card_code=card.code,
        verification_code="330119",
        email_id=99,
    )
    completed_again = store.complete_registration_claim(
        pending.id,
        card_code=card.code,
        verification_code="330119",
        email_id=99,
    )

    assert completed.status == "completed"
    assert completed_again.status == "completed"
    assert store.get_card_by_code(card.code).remaining_uses == 1
    assert [tag.name for tag in store.list_mapping_tags(first.id)] == ["GPT"]
    saved_tag = store.get_tag(gpt.id)
    assert saved_tag is not None
    assert saved_tag.count == 1
    assert saved_tag.success_count == 1
    assert store.count_verification_events(tag_id=gpt.id) == 1
    assert store.start_registration_claim(card.code, address_mode="primary").mapping_id == second.id


def test_alias_mode_can_repeat_platform_but_never_reissues_old_alias(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    root = store.create_mapping("alias-source@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id, address_mode="icloud_alias", uses=3)

    first = store.start_registration_claim(card.code)
    store.complete_registration_claim(
        first.id,
        card_code=card.code,
        verification_code="111111",
        email_id=101,
    )
    second = store.start_registration_claim(card.code)

    assert first.root_mapping_id == root.id == second.root_mapping_id
    assert first.recipient_email != second.recipient_email
    assert "+" in first.recipient_email and "+" in second.recipient_email
    superseded_first = store.get_registration_claim(first.id)
    assert superseded_first is not None and superseded_first.superseded_at
    with pytest.raises(ValueError, match="registration claim is superseded"):
        store.record_registration_claim_code(
            first.id,
            verification_code="222222",
            email_id=102,
        )

    store.skip_registration_claim(second.id, card_code=card.code)
    third = store.start_registration_claim(card.code)
    assert third.recipient_email not in {first.recipient_email, second.recipient_email}
    assert store.get_card_by_code(card.code).remaining_uses == 2
    assert store.get_by_id(second.mapping_id).reuse_policy == "retired"


def test_new_alias_supersedes_an_older_primary_claim_in_the_same_family(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    root = store.create_mapping("primary-then-alias@icloud.com")
    gpt = store.create_tag("GPT")
    _primary_batch, primary_card = _create_batch(store, tag_id=gpt.id, address_mode="primary")
    _alias_batch, alias_card = _create_batch(store, tag_id=gpt.id, address_mode="icloud_alias")

    primary_claim = store.start_registration_claim(primary_card.code)
    store.complete_registration_claim(
        primary_claim.id,
        card_code=primary_card.code,
        verification_code="111111",
        email_id=1101,
    )
    alias_claim = store.start_registration_claim(alias_card.code)

    assert alias_claim.root_mapping_id == root.id
    frozen_primary = store.get_registration_claim(primary_claim.id)
    assert frozen_primary is not None and frozen_primary.superseded_at
    with pytest.raises(ValueError, match="registration claim is superseded"):
        store.record_registration_claim_code(
            primary_claim.id,
            verification_code="222222",
            email_id=1102,
        )


def test_new_primary_supersedes_an_older_alias_claim_in_the_same_family(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    root = store.create_mapping("alias-then-primary@icloud.com")
    gpt = store.create_tag("GPT")
    claude = store.create_tag("Claude")
    _alias_batch, alias_card = _create_batch(store, tag_id=gpt.id, address_mode="icloud_alias")
    _primary_batch, primary_card = _create_batch(store, tag_id=claude.id, address_mode="primary")

    alias_claim = store.start_registration_claim(alias_card.code)
    store.complete_registration_claim(
        alias_claim.id,
        card_code=alias_card.code,
        verification_code="333333",
        email_id=1201,
    )
    primary_claim = store.start_registration_claim(primary_card.code)

    assert primary_claim.root_mapping_id == root.id
    frozen_alias = store.get_registration_claim(alias_claim.id)
    assert frozen_alias is not None and frozen_alias.superseded_at
    with pytest.raises(ValueError, match="registration claim is superseded"):
        store.record_registration_claim_code(
            alias_claim.id,
            verification_code="444444",
            email_id=1202,
        )


def test_public_claim_skips_family_held_by_admin_alias(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    first_root = store.create_mapping("admin-live@icloud.com")
    second_root = store.create_mapping("public-safe@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id, address_mode="icloud_alias")

    admin_alias = store.claim_next_available_mapping(
        target_site=gpt.name,
        claimed_by="admin:live-worker",
        address_mode="icloud_alias",
    )
    public_claim = store.start_registration_claim(card.code)

    assert admin_alias is not None
    assert admin_alias.parent_mapping_id == first_root.id
    assert public_claim.root_mapping_id == second_root.id


def test_admin_claim_supersedes_completed_public_live_claim(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    root = store.create_mapping("public-then-admin@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id, address_mode="icloud_alias")
    public_claim = store.start_registration_claim(card.code)
    store.complete_registration_claim(
        public_claim.id,
        card_code=card.code,
        verification_code="555555",
        email_id=1301,
    )

    admin_alias = store.claim_next_available_mapping(
        target_site=gpt.name,
        claimed_by="admin:next-owner",
        address_mode="icloud_alias",
    )

    assert admin_alias is not None
    assert admin_alias.parent_mapping_id == root.id
    frozen_public_claim = store.get_registration_claim(public_claim.id)
    assert frozen_public_claim is not None and frozen_public_claim.superseded_at
    with pytest.raises(ValueError, match="registration claim is superseded"):
        store.record_registration_claim_code(
            public_claim.id,
            verification_code="666666",
            email_id=1302,
        )


def test_three_consecutive_no_code_skips_start_fifteen_minute_cooldown(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    for index in range(3):
        store.create_mapping(f"cooldown-{index}@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id, uses=5)

    for _ in range(3):
        claim = store.start_registration_claim(card.code)
        store.skip_registration_claim(claim.id, card_code=card.code)

    cooled_card = store.get_card_by_code(card.code)
    assert cooled_card is not None
    assert cooled_card.remaining_uses == 5
    assert cooled_card.consecutive_skips == 0
    assert cooled_card.cooldown_until
    with pytest.raises(ValueError, match="card is cooling down"):
        store.start_registration_claim(card.code)


def test_timeout_does_not_charge_tag_or_count_as_skip_and_retires_alias(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    root = store.create_mapping("timeout-alias@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id, address_mode="icloud_alias", uses=4)
    claim = store.start_registration_claim(card.code)
    with store._connect() as connection:
        connection.execute(
            "UPDATE registration_claims SET created_at = '2000-01-01 00:00:00' WHERE id = ?",
            (claim.id,),
        )
        connection.commit()

    expired = store.expire_registration_claims(timeout_minutes=30)
    refreshed_claim = store.get_registration_claim(claim.id)
    refreshed_card = store.get_card_by_code(card.code)

    assert expired == 1
    assert refreshed_claim is not None and refreshed_claim.status == "timed_out"
    assert refreshed_card is not None
    assert refreshed_card.remaining_uses == 4
    assert refreshed_card.consecutive_skips == 0
    assert refreshed_card.cooldown_until == ""
    assert store.list_mapping_tags(root.id) == []
    assert store.get_by_id(root.id).first_used_at == ""
    assert store.get_by_id(root.id).reuse_policy == "reusable"
    assert store.get_by_id(claim.mapping_id).reuse_policy == "retired"


def test_skipping_primary_address_advances_but_alias_mode_can_keep_root(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    first = store.create_mapping("first-skip@icloud.com")
    second = store.create_mapping("second-skip@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id, uses=3)

    skipped = store.start_registration_claim(card.code)
    assert skipped.mapping_id == first.id
    store.skip_registration_claim(skipped.id, card_code=card.code)

    next_claim = store.start_registration_claim(card.code)
    assert next_claim.mapping_id == second.id


def test_independent_mailbox_uses_only_pristine_family_and_adds_no_service_tag(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    platform_tagged = store.create_mapping("platform-tagged@icloud.com")
    fresh = store.create_mapping("fresh@icloud.com", category="库存 A")
    old_tag = store.create_tag("Claude")
    store.add_mapping_tag(platform_tagged.id, old_tag.id, source="manual")
    system_tag = store.ensure_independent_system_tag()
    _batch, card = _create_batch(
        store,
        tag_id=system_tag.id,
        delivery_mode="independent",
        source_scope="never_used",
    )

    claim = store.start_registration_claim(card.code)
    assert claim.mapping_id == fresh.id
    completed = store.complete_registration_claim(
        claim.id,
        card_code=card.code,
        verification_code="888888",
    )

    assert completed.status == "completed"
    assert store.get_by_id(fresh.id).reuse_policy == "independent"
    assert [tag.name for tag in store.list_mapping_tags(fresh.id)] == ["库存 A"]


def test_never_used_scope_excludes_manual_platform_tags_but_keeps_business_tags(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    platform_tagged = store.create_mapping("platform-tagged@icloud.com")
    business_tagged = store.create_mapping("business-tagged@icloud.com")
    platform_tag = store.create_tag("OpenAI", kind="service")
    business_tag = store.create_tag("库存 A", kind="business")
    target_tag = store.create_tag("Grok", kind="service")
    store.add_mapping_tag(platform_tagged.id, platform_tag.id, source="manual")
    store.add_mapping_tag(business_tagged.id, business_tag.id, source="manual")
    _batch, card = _create_batch(store, tag_id=target_tag.id, source_scope="never_used")

    claim = store.start_registration_claim(card.code)

    assert claim.mapping_id == business_tagged.id
    assert not store.is_mapping_fully_unused(platform_tagged.id)
    assert store.is_mapping_fully_unused(business_tagged.id)


def test_used_reusable_scope_includes_manual_platform_tags(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    platform_tagged = store.create_mapping("platform-tagged@icloud.com")
    store.create_mapping("pristine@icloud.com")
    platform_tag = store.create_tag("OpenAI", kind="service")
    target_tag = store.create_tag("Grok", kind="service")
    store.add_mapping_tag(platform_tagged.id, platform_tag.id, source="manual")
    _batch, card = _create_batch(store, tag_id=target_tag.id, source_scope="used_reusable")

    claim = store.start_registration_claim(card.code)

    assert claim.mapping_id == platform_tagged.id


def test_claim_history_uses_opaque_token_and_can_be_revoked(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping("history@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id)
    claim = store.start_registration_claim(card.code)

    assert claim.view_token
    assert claim.access_key == ""
    assert claim.view_token != mapping.access_key
    assert store.get_registration_claim_by_token(claim.id, claim.view_token) is not None
    assert store.get_registration_claim_by_token(claim.id, "wrong-token") is None

    store.revoke_registration_claim(claim.id)
    assert store.get_registration_claim_by_token(claim.id, claim.view_token) is None
    assert store.get_by_id(mapping.id).status == "idle"


def test_mapping_with_registration_history_cannot_be_deleted(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping("protected-history@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id)
    claim = store.start_registration_claim(card.code)

    with pytest.raises(ValueError, match="mapping has registration history"):
        store.delete_mapping(mapping.id)

    assert store.get_by_id(mapping.id) is not None
    assert store.get_registration_claim(claim.id) is not None


def test_legacy_workbench_completion_updates_usage_tag_and_privacy_policy(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping("admin-workbench@icloud.com", category="待注册")
    claimed = store.claim_next_available_mapping(
        category_filter="待注册",
        claimed_by="admin:test-session",
    )
    assert claimed is not None and claimed.id == mapping.id
    gpt = store.create_tag("GPT")

    completed = store.complete_workbench_mapping(
        mapping.id,
        target_tag_id=gpt.id,
        claimed_by="admin:test-session",
        verification_source="admin_workbench",
        email_id=501,
        prevent_reuse=True,
    )

    assert completed.first_used_at
    assert completed.reuse_policy == "independent"
    assert completed.category == "待注册"
    assert {tag.name for tag in store.list_mapping_tags(mapping.id)} == {"待注册", "GPT"}
    events = store.list_verification_events(tag_id=gpt.id)
    assert len(events) == 1
    assert events[0].source == "admin_workbench"
    assert events[0].email_id == 501


def test_manual_tag_and_display_do_not_create_success_event(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    mapping = store.create_mapping("manual-tag@icloud.com")
    tag = store.create_tag("Claude")

    store.add_mapping_tag(mapping.id, tag.id, source="manual")

    refreshed_tag = store.get_tag(tag.id)
    assert refreshed_tag is not None
    assert refreshed_tag.count == 1
    assert refreshed_tag.success_count == 0
    assert store.count_verification_events(tag_id=tag.id) == 0


def test_mapping_category_defaults_to_business_tag_not_platform(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    mapping = store.create_mapping("legacy-stock@example.com", category="未使用")
    tag_id = store.get_category_id("未使用")
    tag = store.get_tag(tag_id) if tag_id is not None else None

    assert mapping.category == "未使用"
    assert tag is not None
    assert tag.kind == "system"


def test_renaming_platform_tag_keeps_active_workbench_claim_completable(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_tag("库存", kind="business")
    mapping = store.create_mapping("rename-active@example.com", category="库存")
    platform = store.create_tag("GPT", kind="service")
    claimed = store.claim_next_available_mapping(
        category_filter="库存",
        target_site=platform.name,
        claimed_by="admin:rename-test",
    )
    assert claimed is not None and claimed.id == mapping.id

    renamed = store.rename_tag(platform.id, "ChatGPT", kind="service")
    refreshed = store.get_by_id(mapping.id)

    assert renamed.id == platform.id
    assert refreshed is not None and refreshed.target_site == "ChatGPT"
    completed = store.complete_workbench_mapping(
        mapping.id,
        target_tag_id=renamed.id,
        claimed_by="admin:rename-test",
        verification_source="admin_workbench",
        email_id=1401,
    )
    assert completed.status == "idle"
    assert store.count_verification_events(tag_id=renamed.id) == 1


def test_completed_public_claim_history_is_backfilled_without_guessing_manual_tags(tmp_path) -> None:
    database = tmp_path / "app.db"
    store = KeyStore(database)
    mapping = store.create_mapping("backfill@icloud.com")
    gpt = store.create_tag("GPT")
    manual = store.create_tag("库存")
    store.add_mapping_tag(mapping.id, manual.id, source="manual")
    _batch, card = _create_batch(store, tag_id=gpt.id)
    claim = store.start_registration_claim(card.code)
    store.complete_registration_claim(
        claim.id,
        card_code=card.code,
        verification_code="123456",
        email_id=700,
    )
    with store._connect() as connection:
        connection.execute("DELETE FROM verification_events")
        connection.commit()

    migrated = KeyStore(database)

    assert migrated.count_verification_events(tag_id=gpt.id) == 1
    assert migrated.count_verification_events(tag_id=manual.id) == 0


def test_completed_live_claim_records_each_new_mail_once_without_charging_card(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping("follow-up@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id, address_mode="icloud_alias", uses=2)
    claim = store.start_registration_claim(card.code)
    store.complete_registration_claim(
        claim.id,
        card_code=card.code,
        verification_code="101010",
        email_id=801,
    )

    store.record_registration_claim_code(
        claim.id,
        verification_code="202020",
        email_id=802,
    )
    store.record_registration_claim_code(
        claim.id,
        verification_code="202020",
        email_id=802,
    )

    refreshed = store.get_registration_claim(claim.id)
    assert refreshed is not None
    assert refreshed.verification_code == "202020"
    assert refreshed.email_id == 802
    assert store.count_verification_events(tag_id=gpt.id) == 2
    assert store.get_card_by_code(card.code).remaining_uses == 1


def test_public_snapshot_finalization_is_idempotent(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    store.create_mapping("public-snapshot-idempotent@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id)
    claim = store.start_registration_claim(card.code, defer_email_baseline=True)

    assert not claim.baseline_ready
    first = store.update_registration_claim_baseline(claim.id, 100)
    second = store.update_registration_claim_baseline(claim.id, 200)

    assert first.baseline_ready
    assert second.baseline_ready
    assert second.baseline_email_id == 100


def test_migration_preserves_pending_legacy_public_claim_boundary(tmp_path) -> None:
    database = tmp_path / "legacy-public-snapshot.db"
    store = KeyStore(database)
    store.create_mapping("legacy-public@icloud.com")
    gpt = store.create_tag("GPT")
    _batch, card = _create_batch(store, tag_id=gpt.id)
    claim = store.start_registration_claim(card.code)

    assert claim.baseline_ready

    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE registration_claims DROP COLUMN baseline_ready")
        connection.commit()

    migrated = KeyStore(database)
    refreshed = migrated.get_registration_claim(claim.id)

    assert refreshed is not None
    assert refreshed.baseline_ready
