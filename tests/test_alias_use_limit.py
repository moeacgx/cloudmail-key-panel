from __future__ import annotations

import pytest

from app.store import KeyStore


def _create_alias_card(store: KeyStore, *, tag_id: int):
    category = store.create_card_category("裂变限额测试")
    _batch, cards = store.create_card_batch(
        name="裂变限额批次",
        category_id=category.id,
        target_tag_id=tag_id,
        card_count=1,
        uses_per_card=10,
        delivery_mode="custom",
        address_mode="icloud_alias",
    )
    return cards[0]


def test_tag_alias_use_limit_is_validated_and_persisted(tmp_path) -> None:
    database = tmp_path / "app.db"
    store = KeyStore(database)

    tag = store.create_tag("GPT", kind="service", alias_use_limit=5)
    assert tag.alias_use_limit == 5

    # 未显式传入时保留原配置，避免普通改名意外清空上限。
    renamed = store.rename_tag(tag.id, "ChatGPT", kind="service")
    assert renamed.alias_use_limit == 5

    updated = store.set_tag_alias_use_limit(tag.id, 2)
    assert updated.alias_use_limit == 2
    assert KeyStore(database).get_tag(tag.id).alias_use_limit == 2

    with pytest.raises(ValueError, match="non-negative integer"):
        store.set_tag_alias_use_limit(tag.id, -1)
    with pytest.raises(ValueError, match="non-negative integer"):
        store.rename_tag(tag.id, "ChatGPT", alias_use_limit="abc")


def test_public_alias_claim_skips_root_after_target_tag_limit(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    first_root = store.create_mapping("first-limit@icloud.com")
    second_root = store.create_mapping("second-limit@icloud.com")
    tag = store.create_tag("GPT", kind="service", alias_use_limit=2)
    card = _create_alias_card(store, tag_id=tag.id)

    first = store.start_registration_claim(card.code)
    assert first.root_mapping_id == first_root.id
    store.skip_registration_claim(first.id, card_code=card.code)

    second = store.start_registration_claim(card.code)
    assert second.root_mapping_id == first_root.id
    store.skip_registration_claim(second.id, card_code=card.code)

    third = store.start_registration_claim(card.code)
    assert third.root_mapping_id == second_root.id

    with store._connect() as connection:
        generated = connection.execute(
            """
            SELECT COUNT(*)
            FROM alias_generation_events
            WHERE root_mapping_id = ? AND tag_id = ?
            """,
            (first_root.id, tag.id),
        ).fetchone()[0]
    assert generated == 2


def test_admin_alias_claim_limit_counts_skipped_alias_and_is_per_tag(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    first_root = store.create_mapping("admin-first-limit@icloud.com")
    second_root = store.create_mapping("admin-second-limit@icloud.com")
    gpt = store.create_tag("GPT", kind="service", alias_use_limit=1)
    claude = store.create_tag("Claude", kind="service", alias_use_limit=1)

    first = store.claim_next_available_mapping(
        target_site=gpt.name,
        claimed_by="admin:first",
        address_mode="icloud_alias",
    )
    assert first is not None and first.parent_mapping_id == first_root.id
    store.reset_mapping_status(first.id, claimed_by="admin:first")

    # 即使跳过流程会清空临时地址的 target_site，生成事件仍会保留并参与限额。
    assert store.get_by_id(first.id).target_site == ""
    second = store.claim_next_available_mapping(
        target_site=gpt.name,
        claimed_by="admin:second",
        address_mode="icloud_alias",
    )
    assert second is not None and second.parent_mapping_id == second_root.id

    # 限额按平台标签分别累计，GPT 用满不影响 Claude 从第一个主邮箱领取。
    other_platform = store.claim_next_available_mapping(
        target_site=claude.name,
        claimed_by="admin:claude",
        address_mode="icloud_alias",
    )
    assert other_platform is not None and other_platform.parent_mapping_id == first_root.id


def test_alias_limit_history_survives_temporary_alias_cleanup(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")
    root = store.create_mapping("cleanup-limit@icloud.com")
    tag = store.create_tag("GPT", kind="service", alias_use_limit=1)

    alias = store.create_icloud_alias(root.id, target_site=tag.name)
    assert store.delete_mappings([alias.id]) == 1
    assert store.get_by_id(alias.id) is None

    with pytest.raises(ValueError, match="alias use limit reached"):
        store.create_icloud_alias(root.id, target_site=tag.name)
