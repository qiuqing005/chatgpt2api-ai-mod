from __future__ import annotations

from copy import deepcopy
import threading
import time
from typing import Any

from services.account_service import account_service
from services.config import config
from services.model_service import model_catalog_service
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol.text_model_aliases import public_text_model_items
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
_models_refresh_lock = threading.Lock()
_models_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_models_auth_tokens: dict[str, list[str]] = {}
_models_auth_token_cursors: dict[str, int] = {}
_models_auth_failure_at = 0.0


def _clear_models_cache() -> None:
    global _models_auth_failure_at
    with _models_cache_lock:
        _models_cache.clear()
        _models_auth_tokens.clear()
        _models_auth_token_cursors.clear()
        _models_auth_failure_at = 0.0


def _cached_models(key: str, max_age: float) -> dict[str, Any] | None:
    cached = _models_cache.get(key)
    if cached is None or time.monotonic() - cached[0] > max_age:
        return None
    return deepcopy(cached[1])


def _store_models(
    key: str,
    result: dict[str, Any],
    model_tokens: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    cached = deepcopy(result)
    with _models_cache_lock:
        _models_cache[key] = (time.monotonic(), cached)
        if key == "auth":
            _models_auth_tokens.clear()
            _models_auth_tokens.update(deepcopy(model_tokens or {}))
            _models_auth_token_cursors.clear()
    return deepcopy(cached)


def _get_cached_models(key: str, max_age: float) -> dict[str, Any] | None:
    with _models_cache_lock:
        return _cached_models(key, max_age)


def _touch_cached_models(key: str) -> dict[str, Any] | None:
    with _models_cache_lock:
        cached = _models_cache.get(key)
        if cached is None:
            return None
        _models_cache[key] = (time.monotonic(), cached[1])
        return deepcopy(cached[1])


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


def _load_authenticated_models(
    accounts: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[str]], int] | None:
    merged: dict[str, dict[str, Any]] = {}
    model_tokens: dict[str, list[str]] = {}
    failures = 0
    for account in accounts:
        token = str(account.get("access_token") or "").strip()
        backend = None
        try:
            active_token = account_service.refresh_access_token(token, event="models_list") or token
            backend = OpenAIBackendAPI(access_token=active_token)
            result = backend.list_models()
            for item in result.get("data", []):
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get("id") or "").strip()
                if not model_id:
                    continue
                merged.setdefault(model_id, item)
                tokens = model_tokens.setdefault(model_id, [])
                if active_token not in tokens:
                    tokens.append(active_token)
        except Exception:
            failures += 1
            continue
        finally:
            if backend is not None:
                backend.close()

    if not merged:
        return None
    data = [deepcopy(item) for item in merged.values()]
    data.sort(key=lambda item: str(item.get("id") or ""))
    return {"object": "list", "data": data}, model_tokens, failures


def _load_anonymous_models() -> dict[str, Any]:
    backend = OpenAIBackendAPI()
    try:
        return backend.list_models()
    finally:
        backend.close()


def _load_upstream_models(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    global _models_auth_failure_at
    web_accounts = _web_model_accounts(accounts)
    if web_accounts:
        fresh = _get_cached_models("auth", _MODELS_CACHE_TTL_SECS)
        if fresh is not None:
            return fresh
        stale = _get_cached_models("auth", _MODELS_STALE_TTL_SECS)
        with _models_cache_lock:
            failure_recent = (
                _models_auth_failure_at > 0
                and time.monotonic() - _models_auth_failure_at <= _MODELS_AUTH_FAILURE_TTL_SECS
            )
        if not failure_recent:
            with _models_refresh_lock:
                fresh = _get_cached_models("auth", _MODELS_CACHE_TTL_SECS)
                if fresh is not None:
                    return fresh
                stale = _get_cached_models("auth", _MODELS_STALE_TTL_SECS)
                authenticated = _load_authenticated_models(web_accounts)
                if authenticated is not None:
                    result, model_tokens, failures = authenticated
                    if failures and stale is not None:
                        return _touch_cached_models("auth") or stale
                    with _models_cache_lock:
                        _models_auth_failure_at = 0.0
                    return _store_models("auth", result, model_tokens)
                with _models_cache_lock:
                    _models_auth_failure_at = time.monotonic()
        if stale is not None:
            return stale

    fresh = _get_cached_models("anon", _MODELS_CACHE_TTL_SECS)
    if fresh is not None:
        return fresh
    with _models_refresh_lock:
        fresh = _get_cached_models("anon", _MODELS_CACHE_TTL_SECS)
        if fresh is not None:
            return fresh
        try:
            anonymous = _load_anonymous_models()
        except Exception:
            stale = _get_cached_models("anon", _MODELS_STALE_TTL_SECS)
            if stale is not None:
                return stale
            raise
        return _store_models("anon", anonymous)


def _refresh_models_in_background(accounts: list[dict[str, Any]]) -> None:
    if _models_refresh_lock.locked():
        return

    def _run() -> None:
        try:
            _load_upstream_models(accounts)
        except Exception:
            return

    threading.Thread(target=_run, name="refresh-text-models", daemon=True).start()


def preferred_access_token_for_model(model: str, excluded_tokens: set[str] | None = None) -> str:
    model_id = str(model or "").strip()
    if not model_id:
        return ""
    accounts = account_service.list_accounts()
    fresh = _get_cached_models("auth", _MODELS_CACHE_TTL_SECS)
    stale = _get_cached_models("auth", _MODELS_STALE_TTL_SECS)
    if fresh is None and stale is not None:
        _refresh_models_in_background(accounts)
    elif fresh is None:
        try:
            _load_upstream_models(accounts)
        except Exception:
            return ""
    with _models_cache_lock:
        cached = _cached_models("auth", _MODELS_STALE_TTL_SECS)
        candidates = list(_models_auth_tokens.get(model_id, []))
        start = _models_auth_token_cursors.get(model_id, 0) % len(candidates) if candidates else 0
        _models_auth_token_cursors[model_id] = start + 1 if candidates else 0
    if cached is None or not candidates:
        return ""
    excluded = excluded_tokens or set()
    ordered = candidates[start:] + candidates[:start]
    for access_token in ordered:
        resolved = account_service.resolve_access_token(access_token)
        if not resolved or resolved in excluded:
            continue
        account = account_service.get_account(resolved)
        if not isinstance(account, dict):
            continue
        if account.get("status") in {"禁用", "异常"}:
            continue
        if account_service._normalize_source_type(account.get("source_type")) == "codex":
            continue
        return resolved
    return ""


def list_models(*, apply_visibility: bool = True) -> dict[str, Any]:
    accounts = account_service.list_accounts()
    result = _load_upstream_models(accounts)
    data = result.get("data")
    if not isinstance(data, list):
        return result
    data = public_text_model_items(data)
    result["data"] = data
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
    visible_models = config.visible_models if apply_visibility else None
    if visible_models is not None:
        allowed = set(visible_models)
        data = [item for item in data if str(item.get("id") or "").strip() in allowed]
        present = {str(item.get("id") or "").strip() for item in data}
        for model_id in visible_models:
            if model_id in present:
                continue
            data.append({
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "chatgpt2api",
                "permission": [],
                "root": model_id,
                "parent": None,
            })
    result["data"] = data
    return result
