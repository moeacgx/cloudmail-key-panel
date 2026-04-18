import pytest

from app.store import KeyStore


def test_key_store_persists_and_lists_mappings(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    created = store.create_mapping(
        recipient_email="Cranes_Solute.1O@icloud.com",
        query_email=" OpenAI@eve.ink ",
        access_key="demo-key-001",
        label="demo",
    )

    loaded = store.get_by_key("demo-key-001")
    listed = store.list_mappings()

    assert created.recipient_email == "cranes_solute.1o@icloud.com"
    assert created.query_email == "openai@eve.ink"
    assert loaded is not None
    assert loaded.recipient_email == "cranes_solute.1o@icloud.com"
    assert loaded.query_email == "openai@eve.ink"
    assert listed[0].access_key == "demo-key-001"
    assert listed[0].label == "demo"


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


def test_key_store_persists_cloudmail_settings(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    saved = store.save_cloudmail_settings(
        base_url=" https://mail.boxmoe.eu.org/ ",
        api_token=" fixed-token-123 ",
    )
    loaded = store.get_cloudmail_settings()

    assert saved.base_url == "https://mail.boxmoe.eu.org/"
    assert saved.api_token == "fixed-token-123"
    assert loaded.base_url == "https://mail.boxmoe.eu.org/"
    assert loaded.api_token == "fixed-token-123"
