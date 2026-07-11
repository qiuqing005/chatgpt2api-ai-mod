from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Literal

from services.config import config
from services.storage.base import StorageBackend

AuthRole = Literal["admin", "user"]
MAX_IMAGE_QUOTA = 10_000_000
API_IMAGE_RESERVATION_PREFIX = "api:"
API_IMAGE_RESERVATION_TTL_SECS = 3600


class ImageQuotaExceeded(Exception):
    pass


class ImageQuotaStorageError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_api_image_reservation_id() -> str:
    return f"{API_IMAGE_RESERVATION_PREFIX}{int(time.time())}:{uuid.uuid4().hex}"


def _api_reservation_created_at(reservation_id: str) -> int | None:
    if not reservation_id.startswith(API_IMAGE_RESERVATION_PREFIX):
        return None
    parts = reservation_id.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _non_negative_int(value: object, default: int = 0, maximum: int = MAX_IMAGE_QUOTA) -> int:
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return min(max(0, normalized), maximum)


class AuthService:
    def __init__(self, storage: StorageBackend):
        self.storage = storage
        self._lock = Lock()
        self._items = self._load()
        self._recover_api_reservations()
        self._last_used_flush_at: dict[str, datetime] = {}

    @staticmethod
    def _clean(value: object) -> str:
        return str(value or "").strip()

    @staticmethod
    def _default_name(role: object) -> str:
        return "管理员密钥" if str(role or "").strip().lower() == "admin" else "普通用户"

    def _normalize_item(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        role = self._clean(raw.get("role")).lower()
        if role not in {"admin", "user"}:
            return None
        key_hash = self._clean(raw.get("key_hash"))
        if not key_hash:
            return None
        item_id = self._clean(raw.get("id")) or uuid.uuid4().hex[:12]
        name = self._clean(raw.get("name")) or self._default_name(role)
        created_at = self._clean(raw.get("created_at")) or _now_iso()
        last_used_at = self._clean(raw.get("last_used_at")) or None
        image_quota = _non_negative_int(raw.get("image_quota"))
        image_used = _non_negative_int(raw.get("image_used"))
        if image_quota > 0:
            image_used = min(image_used, image_quota)
        raw_reservations = raw.get("image_quota_reservations")
        image_quota_reservations: dict[str, int] = {}
        if isinstance(raw_reservations, dict):
            for reservation_id, amount in raw_reservations.items():
                normalized_id = self._clean(reservation_id)[:256]
                normalized_amount = _non_negative_int(amount)
                if normalized_id and normalized_amount > 0:
                    image_quota_reservations[normalized_id] = normalized_amount
        return {
            "id": item_id,
            "name": name,
            "role": role,
            "key_hash": key_hash,
            "enabled": bool(raw.get("enabled", True)),
            "created_at": created_at,
            "last_used_at": last_used_at,
            "image_quota": image_quota,
            "image_used": image_used,
            "image_quota_reservations": image_quota_reservations,
        }

    def _load(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_auth_keys()
        except Exception as exc:
            raise ImageQuotaStorageError("图片额度存储暂时不可用") from exc
        if not isinstance(items, list):
            return []
        return [normalized for item in items if (normalized := self._normalize_item(item)) is not None]

    def _save(self) -> None:
        self.storage.save_auth_keys(self._items)

    def _recover_api_reservations(self) -> None:
        changed = False
        for index, item in enumerate(self._items):
            reservations = dict(item.get("image_quota_reservations") or {})
            stale_ids = [
                reservation_id
                for reservation_id in reservations
                if reservation_id.startswith(API_IMAGE_RESERVATION_PREFIX)
            ]
            if not stale_ids:
                continue
            refunded = sum(_non_negative_int(reservations.pop(reservation_id, 0)) for reservation_id in stale_ids)
            next_item = dict(item)
            next_item["image_quota_reservations"] = reservations
            next_item["image_used"] = max(0, _non_negative_int(item.get("image_used")) - refunded)
            self._items[index] = next_item
            changed = True
        if changed:
            self._save()

    def _recover_expired_api_reservations_locked(self) -> bool:
        cutoff = int(time.time()) - API_IMAGE_RESERVATION_TTL_SECS
        changed = False
        for index, item in enumerate(self._items):
            reservations = dict(item.get("image_quota_reservations") or {})
            stale_ids = [
                reservation_id
                for reservation_id in reservations
                if (created_at := _api_reservation_created_at(reservation_id)) is not None
                and created_at <= cutoff
            ]
            if not stale_ids:
                continue
            refunded = sum(_non_negative_int(reservations.pop(reservation_id, 0)) for reservation_id in stale_ids)
            next_item = dict(item)
            next_item["image_quota_reservations"] = reservations
            next_item["image_used"] = max(0, _non_negative_int(item.get("image_used")) - refunded)
            self._items[index] = next_item
            changed = True
        return changed

    def _reload_locked(self) -> None:
        self._items = self._load()

    @staticmethod
    def _public_item(item: dict[str, object]) -> dict[str, object]:
        image_quota = _non_negative_int(item.get("image_quota"))
        image_used = _non_negative_int(item.get("image_used"))
        return {
            "id": item.get("id"),
            "name": item.get("name"),
            "role": item.get("role"),
            "enabled": bool(item.get("enabled", True)),
            "created_at": item.get("created_at"),
            "last_used_at": item.get("last_used_at"),
            "image_quota": image_quota,
            "image_used": image_used,
            "image_remaining": max(0, image_quota - image_used) if image_quota > 0 else None,
        }

    def list_keys(self, role: AuthRole | None = None) -> list[dict[str, object]]:
        with self._lock:
            self._reload_locked()
            items = [item for item in self._items if role is None or item.get("role") == role]
            return [self._public_item(item) for item in items]

    def _has_key_hash_locked(self, key_hash: str, *, exclude_id: str = "") -> bool:
        for item in self._items:
            item_id = self._clean(item.get("id"))
            if exclude_id and item_id == exclude_id:
                continue
            stored_hash = self._clean(item.get("key_hash"))
            if stored_hash and hmac.compare_digest(stored_hash, key_hash):
                return True
        return False

    def _build_key_hash_locked(self, raw_key: str, *, exclude_id: str = "") -> str:
        candidate = self._clean(raw_key)
        if not candidate:
            raise ValueError("请输入新的专用密钥")
        admin_key = self._clean(config.auth_key)
        if admin_key and hmac.compare_digest(candidate, admin_key):
            raise ValueError("这个密钥和管理员密钥冲突了，请换一个新的密钥")
        key_hash = _hash_key(candidate)
        if self._has_key_hash_locked(key_hash, exclude_id=exclude_id):
            raise ValueError("这个专用密钥已经存在，请换一个新的密钥")
        return key_hash

    def _has_name_locked(self, name: str, *, role: AuthRole | None = None, exclude_id: str = "") -> bool:
        candidate = self._clean(name)
        if not candidate:
            return False
        for item in self._items:
            item_id = self._clean(item.get("id"))
            if exclude_id and item_id == exclude_id:
                continue
            if role is not None and item.get("role") != role:
                continue
            if self._clean(item.get("name")) == candidate:
                return True
        return False

    def _build_default_name_locked(self, role: AuthRole, *, exclude_id: str = "") -> str:
        base_name = self._default_name(role)
        if not self._has_name_locked(base_name, role=role, exclude_id=exclude_id):
            return base_name
        suffix = 2
        while True:
            candidate = f"{base_name} {suffix}"
            if not self._has_name_locked(candidate, role=role, exclude_id=exclude_id):
                return candidate
            suffix += 1

    def _build_name_locked(self, name: str, *, role: AuthRole, exclude_id: str = "") -> str:
        candidate = self._clean(name)
        if not candidate:
            return self._build_default_name_locked(role, exclude_id=exclude_id)
        if self._has_name_locked(candidate, role=role, exclude_id=exclude_id):
            raise ValueError("这个名称已经在使用中了，换一个更容易区分的名称吧")
        return candidate

    def create_key(self, *, role: AuthRole, name: str = "", image_quota: int = 0) -> tuple[dict[str, object], str]:
        with self._lock:
            self._reload_locked()
            normalized_name = self._build_name_locked(name, role=role)
            while True:
                raw_key = f"sk-{secrets.token_urlsafe(24)}"
                try:
                    key_hash = self._build_key_hash_locked(raw_key)
                    break
                except ValueError:
                    continue
            item = {
                "id": uuid.uuid4().hex[:12],
                "name": normalized_name,
                "role": role,
                "key_hash": key_hash,
                "enabled": True,
                "created_at": _now_iso(),
                "last_used_at": None,
                "image_quota": _non_negative_int(image_quota),
                "image_used": 0,
            }
            self._items.append(item)
            self._save()
            return self._public_item(item), raw_key

    def update_key(
        self,
        key_id: str,
        updates: dict[str, object],
        *,
        role: AuthRole | None = None,
    ) -> dict[str, object] | None:
        normalized_id = self._clean(key_id)
        if not normalized_id:
            return None
        with self._lock:
            self._reload_locked()
            for index, item in enumerate(self._items):
                if item.get("id") != normalized_id:
                    continue
                if role is not None and item.get("role") != role:
                    return None
                next_item = dict(item)
                next_role = "admin" if str(next_item.get("role") or "").strip().lower() == "admin" else "user"
                reservations = dict(next_item.get("image_quota_reservations") or {})
                reserved_total = sum(_non_negative_int(value) for value in reservations.values())
                if "name" in updates and updates.get("name") is not None:
                    next_item["name"] = self._build_name_locked(
                        str(updates.get("name") or ""),
                        role=next_role,
                        exclude_id=normalized_id,
                    )
                if "enabled" in updates and updates.get("enabled") is not None:
                    next_item["enabled"] = bool(updates.get("enabled"))
                if "image_quota" in updates and updates.get("image_quota") is not None:
                    image_quota = _non_negative_int(updates.get("image_quota"))
                    current_used = _non_negative_int(next_item.get("image_used"))
                    if image_quota > 0 and image_quota < current_used:
                        raise ValueError(f"图片额度不能低于当前已使用额度 {current_used}")
                    next_item["image_quota"] = image_quota
                if "image_used" in updates and updates.get("image_used") is not None:
                    image_quota = _non_negative_int(next_item.get("image_quota"))
                    image_used = _non_negative_int(updates.get("image_used")) + reserved_total
                    if image_quota > 0 and image_used > image_quota:
                        raise ValueError(f"图片用量与在途任务合计不能超过当前额度 {image_quota}")
                    next_item["image_used"] = image_used
                if "key" in updates and updates.get("key") is not None:
                    next_item["key_hash"] = self._build_key_hash_locked(str(updates.get("key") or ""), exclude_id=normalized_id)
                self._items[index] = next_item
                self._save()
                return self._public_item(next_item)
        return None

    def reserve_image_quota(
        self,
        identity: dict[str, object],
        amount: int = 1,
        *,
        reservation_id: str = "",
    ) -> bool:
        if identity.get("role") != "user":
            return False
        normalized_id = self._clean(identity.get("id"))
        requested = _non_negative_int(amount)
        normalized_reservation_id = self._clean(reservation_id)[:256]
        if not normalized_id or requested <= 0:
            return False
        with self._lock:
            self._reload_locked()
            if self._recover_expired_api_reservations_locked():
                self._save()
            for index, item in enumerate(self._items):
                if item.get("id") != normalized_id or item.get("role") != "user":
                    continue
                quota = _non_negative_int(item.get("image_quota"))
                if quota <= 0:
                    return False
                reservations = dict(item.get("image_quota_reservations") or {})
                if normalized_reservation_id and normalized_reservation_id in reservations:
                    return True
                used = min(_non_negative_int(item.get("image_used")), quota)
                remaining = quota - used
                if requested > remaining:
                    raise ImageQuotaExceeded(f"图片生成额度不足（剩余 {remaining}，请求 {requested}）")
                next_item = dict(item)
                next_item["image_used"] = used + requested
                if normalized_reservation_id:
                    reservations[normalized_reservation_id] = requested
                    next_item["image_quota_reservations"] = reservations
                self._items[index] = next_item
                self._save()
                return True
        raise ImageQuotaStorageError("用户密钥已不存在，无法预留图片额度")

    def refund_image_quota(
        self,
        owner_id: str,
        amount: int = 1,
        *,
        reservation_id: str = "",
    ) -> bool:
        normalized_id = self._clean(owner_id)
        refunded = _non_negative_int(amount)
        normalized_reservation_id = self._clean(reservation_id)[:256]
        if not normalized_id or refunded <= 0:
            return False
        with self._lock:
            self._reload_locked()
            for index, item in enumerate(self._items):
                if item.get("id") != normalized_id or item.get("role") != "user":
                    continue
                next_item = dict(item)
                if normalized_reservation_id:
                    reservations = dict(item.get("image_quota_reservations") or {})
                    reserved_amount = _non_negative_int(reservations.pop(normalized_reservation_id, 0))
                    if reserved_amount <= 0:
                        return False
                    refunded = reserved_amount
                    next_item["image_quota_reservations"] = reservations
                next_item["image_used"] = max(0, _non_negative_int(item.get("image_used")) - refunded)
                self._items[index] = next_item
                self._save()
                return True
        return False

    def commit_image_quota(self, owner_id: str, *, reservation_id: str) -> bool:
        normalized_id = self._clean(owner_id)
        normalized_reservation_id = self._clean(reservation_id)[:256]
        if not normalized_id or not normalized_reservation_id:
            return False
        with self._lock:
            self._reload_locked()
            for index, item in enumerate(self._items):
                if item.get("id") != normalized_id or item.get("role") != "user":
                    continue
                reservations = dict(item.get("image_quota_reservations") or {})
                if normalized_reservation_id not in reservations:
                    return False
                reservations.pop(normalized_reservation_id, None)
                next_item = dict(item)
                next_item["image_quota_reservations"] = reservations
                self._items[index] = next_item
                self._save()
                return True
        return False

    def settle_image_quota(self, owner_id: str, success_count: int, *, reservation_id: str) -> bool:
        normalized_id = self._clean(owner_id)
        normalized_reservation_id = self._clean(reservation_id)[:256]
        if not normalized_id or not normalized_reservation_id:
            return False
        with self._lock:
            self._reload_locked()
            for index, item in enumerate(self._items):
                if item.get("id") != normalized_id or item.get("role") != "user":
                    continue
                reservations = dict(item.get("image_quota_reservations") or {})
                reserved_amount = _non_negative_int(reservations.pop(normalized_reservation_id, 0))
                if reserved_amount <= 0:
                    return False
                charged = min(reserved_amount, _non_negative_int(success_count))
                next_item = dict(item)
                next_item["image_quota_reservations"] = reservations
                next_item["image_used"] = max(
                    0,
                    _non_negative_int(item.get("image_used")) - (reserved_amount - charged),
                )
                self._items[index] = next_item
                self._save()
                return True
        return False

    def delete_key(self, key_id: str, *, role: AuthRole | None = None) -> bool:
        normalized_id = self._clean(key_id)
        if not normalized_id:
            return False
        with self._lock:
            self._reload_locked()
            before = len(self._items)
            self._items = [
                item
                for item in self._items
                if not (item.get("id") == normalized_id and (role is None or item.get("role") == role))
            ]
            if len(self._items) == before:
                return False
            self._save()
            return True

    def authenticate(self, raw_key: str) -> dict[str, object] | None:
        candidate = self._clean(raw_key)
        if not candidate:
            return None
        candidate_hash = _hash_key(candidate)
        with self._lock:
            for index, item in enumerate(self._items):
                if not bool(item.get("enabled", True)):
                    continue
                stored_hash = self._clean(item.get("key_hash"))
                if not stored_hash or not hmac.compare_digest(stored_hash, candidate_hash):
                    continue
                next_item = dict(item)
                now = datetime.now(timezone.utc)
                next_item["last_used_at"] = now.isoformat()
                self._items[index] = next_item
                item_id = self._clean(next_item.get("id"))
                last_flush_at = self._last_used_flush_at.get(item_id)
                if last_flush_at is None or (now - last_flush_at).total_seconds() >= 60:
                    try:
                        self._save()
                        self._last_used_flush_at[item_id] = now
                    except Exception:
                        pass
                return self._public_item(next_item)
        return None


auth_service = AuthService(config.get_storage_backend())
