from __future__ import annotations

import pytest

from services.storage.json_storage import JSONStorageBackend


def test_json_save_preserves_missing_partial_rows_and_explicit_delete(tmp_path):
    backend = JSONStorageBackend(tmp_path / "accounts.json")
    backend.save_accounts([
        {"access_token": "token-a", "name": "A"},
        {"access_token": "token-b", "name": "B"},
    ])

    backend.save_accounts([{"access_token": "token-a", "name": "A updated"}])
    assert {item["access_token"] for item in backend.load_accounts()} == {"token-a", "token-b"}

    assert backend.delete_accounts(["token-b"]) == 1
    assert [item["access_token"] for item in backend.load_accounts()] == ["token-a"]


def test_json_save_refuses_to_overwrite_malformed_existing_file(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text("{malformed", encoding="utf-8")
    backend = JSONStorageBackend(path)

    with pytest.raises(Exception):
        backend.save_accounts([{"access_token": "token-a"}])

    assert path.read_text(encoding="utf-8") == "{malformed"
