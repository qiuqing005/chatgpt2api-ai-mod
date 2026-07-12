from __future__ import annotations

from copy import deepcopy
import threading
import time
from typing import Any

from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import CODEX_IMAGE_MODEL


_MODEL_ACCOUNT_PRIORITY = {
    "Pro": 0,
    "Team": 1,
    "Enterprise": 2,
    "Plus": 3,
    "ProLite": 4,
    "free": 5,
}
_MODELS_CACHE_TTL_SECS = 300.0
_MODELS_STALE_TTL_SECS = 1800.0
_MODELS_AUTH_FAILURE_TTL_SECS = 60.0
_models_cache_lock = threading.Lock()
_models_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_models_auth_token = ""
_models_auth_failure_at = 0.0


def _clear_models_cache() -> None:
    global _models_auth_failure_at, _models_auth_token
    with _models_cache_lock:
        _models_cache.clear()
        _models_auth_token = ""
        _models_auth_failure_at = 0.0


def _cached_models(key: str, max_age: float) -> dict[str, Any] | None:
    cached = _models_cache.get(key)
    if cached is None or time.monotonic() - cached[0] > max_age:
        return None
    return deepcopy(cached[1])


def _store_models(key: str, result: dict[str, Any], access_token: str = "") -> dict[str, Any]:
    global _models_auth_token
    cached = deepcopy(result)
    _models_cache[key] = (time.monotonic(), cached)
    if key == "auth":
        _models_auth_token = access_token
    return deepcopy(cached)


def _web_model_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        account
        for account in accounts
        if (
            isinstance(account, dict)
            and account.get("access_token")
            and account.get("status") not in {"禁用", "异常"}
            and account_service._normalize_source_type(account.get("source_type")) != "codex"
        )
    ]
    return sorted(
        candidates,
        key=lambda account: _MODEL_ACCOUNT_PRIORITY.get(
            account_service._normalize_account_type(account.get("type")) or "free",
            99,
        ),
    )


def _load_authenticated_models(accounts: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
    for account in accounts:
        token = str(account.get("access_token") or "").strip()
        backend = None
        try:
            active_token = account_service.refresh_access_token(token, event="models_list") or token
            backend = OpenAIBackendAPI(access_token=active_token)
            return backend.list_models(), active_token
        except Exception:
            continue
        finally:
            if backend is not None:
                backend.close()

    return None


def _load_anonymous_models() -> dict[str, Any]:
    backend = OpenAIBackendAPI()
    try:
        return backend.list_models()
    finally:
        backend.close()


def _load_upstream_models(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    global _models_auth_failure_at
    web_accounts = _web_model_accounts(accounts)
    with _models_cache_lock:
        if web_accounts:
            fresh = _cached_models("auth", _MODELS_CACHE_TTL_SECS)
            if fresh is not None:
                return fresh
            failure_recent = (
                _models_auth_failure_at > 0
                and time.monotonic() - _models_auth_failure_at <= _MODELS_AUTH_FAILURE_TTL_SECS
            )
            if not failure_recent:
                authenticated = _load_authenticated_models(web_accounts)
                if authenticated is not None:
                    _models_auth_failure_at = 0.0
                    result, access_token = authenticated
                    return _store_models("auth", result, access_token)
                _models_auth_failure_at = time.monotonic()
            stale = _cached_models("auth", _MODELS_STALE_TTL_SECS)
            if stale is not None:
                return stale

        fresh = _cached_models("anon", _MODELS_CACHE_TTL_SECS)
        if fresh is not None:
            return fresh
        try:
            anonymous = _load_anonymous_models()
        except Exception:
            stale = _cached_models("anon", _MODELS_STALE_TTL_SECS)
            if stale is not None:
                return stale
            raise
        return _store_models("anon", anonymous)


def preferred_access_token_for_model(model: str) -> str:
    model_id = str(model or "").strip()
    if model_id != "gpt-5.6-sol-wm":
        return ""
    if not _models_cache_lock.acquire(blocking=False):
        return ""
    try:
        cached = _cached_models("auth", _MODELS_STALE_TTL_SECS)
        access_token = _models_auth_token
    finally:
        _models_cache_lock.release()
    if cached is None or not access_token:
        return ""
    model_ids = {
        str(item.get("id") or "").strip()
        for item in cached.get("data", [])
        if isinstance(item, dict)
    }
    if model_id not in model_ids:
        return ""
    resolved = account_service.resolve_access_token(access_token)
    account = account_service.get_account(resolved)
    if not isinstance(account, dict):
        return ""
    if account.get("status") in {"禁用", "异常"}:
        return ""
    if account_service._normalize_source_type(account.get("source_type")) == "codex":
        return ""
    return resolved


def list_models() -> dict[str, Any]:
    accounts = account_service.list_accounts()
    result = _load_upstream_models(accounts)
    data = result.get("data")
    if not isinstance(data, list):
        return result
    seen = {str(item.get("id") or "").strip() for item in data if isinstance(item, dict)}
    dynamic_models: set[str] = set()
    web_image_accounts = [
        account
        for account in accounts
        if (
            isinstance(account, dict)
            and account.get("access_token")
            and account_service._normalize_source_type(account.get("source_type")) != "codex"
        )
    ]
    codex_types = {
        normalized
        for account in accounts
        if (
            isinstance(account, dict)
            and account_service._normalize_source_type(account.get("source_type")) == "codex"
            and (normalized := account_service._normalize_account_type(account.get("type")))
        )
    }

    if web_image_accounts:
        dynamic_models.add("gpt-image-2")
    if codex_types & {"Plus", "Team", "Pro"}:
        dynamic_models.add(CODEX_IMAGE_MODEL)
    if "Plus" in codex_types:
        dynamic_models.add(f"plus-{CODEX_IMAGE_MODEL}")
    if "Team" in codex_types:
        dynamic_models.add(f"team-{CODEX_IMAGE_MODEL}")
    if "Pro" in codex_types:
        dynamic_models.add(f"pro-{CODEX_IMAGE_MODEL}")

    for model in sorted(dynamic_models):
        if model not in seen:
            data.append({
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": "chatgpt2api",
                "permission": [],
                "root": model,
                "parent": None,
            })
    return result
