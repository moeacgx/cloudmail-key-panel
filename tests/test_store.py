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


def test_key_store_supports_search_pagination_and_batch_delete(tmp_path) -> None:
    store = KeyStore(tmp_path / "app.db")

    alpha_key = store.create_mapping(
        recipient_email="alpha@example.com",
        query_email="openai@eve.ink",
        access_key="alpha-key-1",
        label="starter",
    )
    beta_key = store.create_mapping(
        recipient_email="beta@example.com",
        query_email="openai@eve.ink",
        access_key="beta-key-1",
        label="alpha label",
    )
    gamma_key = store.create_mapping(
        recipient_email="gamma@example.com",
        query_email="alpha@eve.ink",
        access_key="gamma-key-1",
        label="normal",
    )
    store.create_mapping(
        recipient_email="delta@example.com",
        query_email="mail@eve.ink",
        access_key="delta-key-1",
        label="normal",
    )

    page_one = store.list_mappings(search_query="alpha", limit=2, offset=0)
    page_two = store.list_mappings(search_query="alpha", limit=2, offset=2)
    total = store.count_mappings(search_query="alpha")

    assert total == 3
    assert [item.access_key for item in page_one] == ["gamma-key-1", "beta-key-1"]
    assert [item.access_key for item in page_two] == ["alpha-key-1"]

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
    )
    loaded = store.get_cloudmail_settings(default_recent_email_limit=10)

    assert saved.base_url == "https://mail.boxmoe.eu.org/"
    assert saved.api_token == "fixed-token-123"
    assert saved.internal_admin_email == "admin@example.com"
    assert saved.internal_admin_password == "secret"
    assert saved.default_query_email == "openai@eve.ink"
    assert saved.recent_email_limit == 3
    assert loaded.base_url == "https://mail.boxmoe.eu.org/"
    assert loaded.api_token == "fixed-token-123"
    assert loaded.internal_admin_email == "admin@example.com"
    assert loaded.internal_admin_password == "secret"
    assert loaded.default_query_email == "openai@eve.ink"
    assert loaded.recent_email_limit == 3
