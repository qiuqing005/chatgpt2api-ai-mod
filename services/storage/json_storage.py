from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from services.storage.base import StorageBackend


class JSONStorageBackend(StorageBackend):
    """本地 JSON 文件存储后端"""

    def __init__(self, file_path: Path, auth_keys_path: Path | None = None):
        self.file_path = file_path
        self.auth_keys_path = auth_keys_path or file_path.with_name("auth_keys.json")
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth_keys_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load_json_list(file_path: Path) -> list[dict[str, Any]]:
        if not file_path.exists():
            return []
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, Exception):
            return []

    @staticmethod
    def _load_json_list_strict(file_path: Path) -> list[dict[str, Any]]:
        if not file_path.exists():
            return []
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"{file_path.name} must contain a JSON array")
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _load_auth_keys_strict(file_path: Path) -> list[dict[str, Any]]:
        if not file_path.exists():
            return []
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("items")
        if not isinstance(data, list):
            raise ValueError(f"{file_path.name} must contain an items array")
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _save_json_list(file_path: Path, items: list[dict[str, Any]]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        JSONStorageBackend._atomic_write(file_path, json.dumps(items, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _atomic_write(file_path: Path, content: str) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{file_path.name}.", suffix=".tmp", dir=file_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, file_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def load_accounts(self) -> list[dict[str, Any]]:
        """从 JSON 文件加载账号数据"""
        return self._load_json_list(self.file_path)

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """增量 upsert 账号数据，不因不完整内存快照删除旧账号。"""
        existing = self._load_json_list_strict(self.file_path)
        merged = {
            str(item.get("access_token") or "").strip(): item
            for item in existing
            if str(item.get("access_token") or "").strip()
        }
        seen: set[str] = set()
        for item in accounts:
            if not isinstance(item, dict):
                continue
            token = str(item.get("access_token") or "").strip()
            if not token:
                continue
            if token in seen:
                raise ValueError("Duplicate access_token in storage snapshot")
            seen.add(token)
            merged[token] = item
        self._save_json_list(self.file_path, list(merged.values()))

    def delete_accounts(self, access_tokens: list[str]) -> int:
        tokens = {str(token or "").strip() for token in access_tokens if str(token or "").strip()}
        if not tokens:
            return 0
        existing = self._load_json_list_strict(self.file_path)
        remaining = [
            item for item in existing
            if str(item.get("access_token") or "").strip() not in tokens
        ]
        removed = len(existing) - len(remaining)
        if removed:
            self._save_json_list(self.file_path, remaining)
        return removed

    def load_auth_keys(self) -> list[dict[str, Any]]:
        """从 JSON 文件加载鉴权密钥数据"""
        if not self.auth_keys_path.exists():
            return []
        try:
            data = json.loads(self.auth_keys_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return []
        if isinstance(data, dict):
            data = data.get("items")
        return data if isinstance(data, list) else []

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """增量 upsert 鉴权密钥，不因不完整内存快照删除旧密钥。"""
        existing = self._load_auth_keys_strict(self.auth_keys_path)
        merged = {
            str(item.get("id") or "").strip(): item
            for item in existing
            if str(item.get("id") or "").strip()
        }
        seen: set[str] = set()
        for item in auth_keys:
            if not isinstance(item, dict):
                continue
            key_id = str(item.get("id") or "").strip()
            if not key_id:
                continue
            if key_id in seen:
                raise ValueError("Duplicate id in storage snapshot")
            seen.add(key_id)
            merged[key_id] = item
        self._atomic_write(
            self.auth_keys_path,
            json.dumps({"items": list(merged.values())}, ensure_ascii=False, indent=2) + "\n",
        )

    def delete_auth_keys(self, key_ids: list[str]) -> int:
        ids = {str(key_id or "").strip() for key_id in key_ids if str(key_id or "").strip()}
        if not ids:
            return 0
        existing = self._load_auth_keys_strict(self.auth_keys_path)
        remaining = [item for item in existing if str(item.get("id") or "").strip() not in ids]
        removed = len(existing) - len(remaining)
        if removed:
            self._atomic_write(
                self.auth_keys_path,
                json.dumps({"items": remaining}, ensure_ascii=False, indent=2) + "\n",
            )
        return removed

    def health_check(self) -> dict[str, Any]:
        """健康检查"""
        try:
            # Parse both files so a corrupt store is reported before it can be mistaken for an empty store.
            self._load_json_list_strict(self.file_path)
            self._load_auth_keys_strict(self.auth_keys_path)
            return {
                "status": "healthy",
                "backend": "json",
                "file_exists": self.file_path.exists(),
                "file_path": str(self.file_path),
                "auth_keys_file_exists": self.auth_keys_path.exists(),
                "auth_keys_file_path": str(self.auth_keys_path),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "json",
                "error": str(e),
            }

    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        return {
            "type": "json",
            "description": "本地 JSON 文件存储",
            "file_path": str(self.file_path),
            "file_exists": self.file_path.exists(),
            "auth_keys_file_path": str(self.auth_keys_path),
            "auth_keys_file_exists": self.auth_keys_path.exists(),
        }
