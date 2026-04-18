import pytest

from app.store import KeyStore


def test_key_store_persists_and_lists_mappings(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    created = store.create_mapping(
        recipient_email="Cranes_Solute.1O@icloud.com",
        access_key="demo-key-001",
        label="demo",
    )

    loaded = store.get_by_key("demo-key-001")
    listed = store.list_mappings()

    assert created.recipient_email == "cranes_solute.1o@icloud.com"
    assert loaded is not None
    assert loaded.recipient_email == "cranes_solute.1o@icloud.com"
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
