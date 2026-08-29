import json

import pytest

from services.storage.database_storage import (
    AccountModel,
    AuthKeyModel,
    DatabaseStorageBackend,
)


def _account_rows(backend: DatabaseStorageBackend) -> dict[str, AccountModel]:
    session = backend.Session()
    try:
        return {
            row.access_token: row
            for row in session.query(AccountModel).order_by(AccountModel.id).all()
        }
    finally:
        session.close()


def test_save_accounts_preserves_existing_rows_and_updates_only_changed_data(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'accounts.db'}")
    backend.save_accounts(
        [
            {"access_token": "token-a", "name": "A"},
            {"access_token": "token-b", "name": "B"},
        ]
    )

    before = _account_rows(backend)
    before_ids = {token: row.id for token, row in before.items()}
    before_b_data = before["token-b"].data

    backend.save_accounts(
        [
            {"access_token": "token-b", "name": "B"},
            {"access_token": "token-a", "name": "A updated"},
            {"access_token": "token-c", "name": "C"},
        ]
    )

    after = _account_rows(backend)
    assert set(after) == {"token-a", "token-b", "token-c"}
    assert after["token-a"].id == before_ids["token-a"]
    assert after["token-b"].id == before_ids["token-b"]
    assert after["token-b"].data == before_b_data
    assert json.loads(after["token-a"].data)["name"] == "A updated"


def test_save_accounts_preserves_rows_missing_from_partial_snapshot(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'accounts.db'}")
    backend.save_accounts(
        [
            {"access_token": "token-a", "name": "A"},
            {"access_token": "token-b", "name": "B"},
        ]
    )
    before = _account_rows(backend)
    token_a_id = before["token-a"].id

    backend.save_accounts([{"access_token": "token-b", "name": "B"}])

    after = _account_rows(backend)
    assert set(after) == {"token-a", "token-b"}
    assert after["token-a"].id == token_a_id


def test_delete_accounts_is_explicit(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'accounts.db'}")
    backend.save_accounts([
        {"access_token": "token-a", "name": "A"},
        {"access_token": "token-b", "name": "B"},
    ])

    assert backend.delete_accounts(["token-a"]) == 1
    assert [item["access_token"] for item in backend.load_accounts()] == ["token-b"]


def test_save_accounts_supports_long_tokens_without_indexing_full_value(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'long-token.db'}")
    long_token = "token-" + ("x" * 5000)

    backend.save_accounts([{"access_token": long_token, "name": "long"}])

    assert backend.load_accounts() == [{"access_token": long_token, "name": "long"}]
    session = backend.Session()
    try:
        row = session.query(AccountModel).one()
        assert len(row.access_token_hash) == 64
    finally:
        session.close()


def test_existing_accounts_table_gets_token_hash_column(tmp_path):
    from sqlalchemy import create_engine, text

    database_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE accounts ("
                "id INTEGER PRIMARY KEY, access_token TEXT NOT NULL, data TEXT NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO accounts (id, access_token, data) VALUES (1, 'legacy-token', '{}')")
        )

    backend = DatabaseStorageBackend(database_url)

    session = backend.Session()
    try:
        row = session.query(AccountModel).one()
        assert len(row.access_token_hash) == 64
    finally:
        session.close()


def test_save_accounts_rejects_duplicate_tokens_and_rolls_back(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'accounts.db'}")
    original = {"access_token": "token-a", "name": "A"}
    backend.save_accounts([original])

    with pytest.raises(ValueError, match="Duplicate access_token") as exc_info:
        backend.save_accounts(
            [
                {"access_token": "token-a", "name": "first update"},
                {"access_token": "token-a", "name": "second update"},
            ]
        )

    assert "token-a" not in str(exc_info.value)
    assert backend.load_accounts() == [original]


def test_save_accounts_rejects_duplicate_new_tokens_and_rolls_back(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'accounts.db'}")
    original = {"access_token": "token-a", "name": "A"}
    backend.save_accounts([original])

    with pytest.raises(ValueError, match="Duplicate access_token") as exc_info:
        backend.save_accounts(
            [
                original,
                {"access_token": "token-b", "name": "first new"},
                {"access_token": "token-b", "name": "second new"},
            ]
        )

    assert "token-b" not in str(exc_info.value)
    assert backend.load_accounts() == [original]


def test_save_auth_keys_preserves_ids_with_target_key_mapping(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'auth-keys.db'}")
    backend.save_auth_keys(
        [
            {"id": "key-a", "name": "A"},
            {"id": "key-b", "name": "B"},
        ]
    )

    session = backend.Session()
    try:
        before_ids = {
            row.key_id: row.id
            for row in session.query(AuthKeyModel).order_by(AuthKeyModel.id).all()
        }
    finally:
        session.close()

    backend.save_auth_keys(
        [
            {"id": "key-b", "name": "B"},
            {"id": "key-a", "name": "A updated"},
            {"id": "key-c", "name": "C"},
        ]
    )

    session = backend.Session()
    try:
        after = {
            row.key_id: row
            for row in session.query(AuthKeyModel).order_by(AuthKeyModel.id).all()
        }
        assert set(after) == {"key-a", "key-b", "key-c"}
        assert after["key-a"].id == before_ids["key-a"]
        assert after["key-b"].id == before_ids["key-b"]
        assert json.loads(after["key-a"].data)["name"] == "A updated"
    finally:
        session.close()


def test_save_auth_keys_rejects_duplicate_ids_and_rolls_back(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'auth-keys.db'}")
    original = {"id": "key-a", "name": "A"}
    backend.save_auth_keys([original])

    with pytest.raises(ValueError, match="Duplicate id") as exc_info:
        backend.save_auth_keys(
            [
                {"id": "key-a", "name": "first update"},
                {"id": "key-a", "name": "second update"},
            ]
        )

    assert "key-a" not in str(exc_info.value)
    assert backend.load_auth_keys() == [original]


def test_save_auth_keys_preserves_missing_partial_rows(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'auth-keys.db'}")
    backend.save_auth_keys([
        {"id": "key-a", "name": "A"},
        {"id": "key-b", "name": "B"},
    ])

    backend.save_auth_keys([{"id": "key-a", "name": "A updated"}])

    assert {item["id"] for item in backend.load_auth_keys()} == {"key-a", "key-b"}
    assert backend.delete_auth_keys(["key-b"]) == 1
    assert [item["id"] for item in backend.load_auth_keys()] == ["key-a"]
