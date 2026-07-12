from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


TEXT_THINKING_EFFORTS = {
    "low": "min",
    "medium": "standard",
    "high": "extended",
    "xhigh": "max",
}


@dataclass(frozen=True)
class TextModelAlias:
    backend_model: str
    thinking_model: str = ""


TEXT_MODEL_ALIASES = {
    "gpt-5.3": TextModelAlias("gpt-5-3"),
    "gpt-5.4": TextModelAlias("gpt-5-4-thinking", "gpt-5-4-thinking"),
    "gpt-5.5": TextModelAlias("gpt-5-5", "gpt-5-5-thinking"),
    "gpt-5.6": TextModelAlias("gpt-5-6-thinking", "gpt-5-6-thinking"),
    "gpt-5.6-luna": TextModelAlias("gpt-5.6-luna-wm", "gpt-5.6-luna-wm"),
    "gpt-5.6-terra": TextModelAlias("gpt-5.6-terra-wm", "gpt-5.6-terra-wm"),
    "gpt-5.6-sol": TextModelAlias("gpt-5.6-sol-wm", "gpt-5.6-sol-wm"),
    "gpt-5-mini": TextModelAlias("gpt-5-5-mini"),
    "gpt-5-pro": TextModelAlias("gpt-5-6-pro"),
}

_PUBLIC_MODEL_SOURCES = {
    "gpt-5.3": ("gpt-5-3",),
    "gpt-5.4": ("gpt-5-4-thinking",),
    "gpt-5.5": ("gpt-5-5", "gpt-5-5-thinking"),
    "gpt-5.6-luna": ("gpt-5.6-luna-wm",),
    "gpt-5.6-terra": ("gpt-5.6-terra-wm",),
    "gpt-5.6-sol": ("gpt-5.6-sol-wm",),
    "gpt-5-mini": ("gpt-5-5-mini",),
    "gpt-5-pro": ("gpt-5-6-pro",),
}
_PASSTHROUGH_PUBLIC_MODELS = ("o3", "o3-pro", "research")


def normalize_text_thinking_effort(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "none"}:
        return ""
    if normalized in {"low", "min"}:
        return "low"
    if normalized in {"medium", "standard"}:
        return "medium"
    if normalized in {"high", "extended"}:
        return "high"
    if normalized in {"xhigh", "max"}:
        return "xhigh"
    return ""


def backend_thinking_effort(value: object) -> str:
    return TEXT_THINKING_EFFORTS.get(normalize_text_thinking_effort(value), "")


def resolve_text_backend_route(model: object, thinking_effort: object = "") -> tuple[str, str]:
    requested_model = str(model or "").strip() or "auto"
    normalized_model = requested_model.lower()
    suffix_effort = ""
    base_model = normalized_model

    for suffix in TEXT_THINKING_EFFORTS:
        marker = f"-{suffix}"
        if normalized_model.endswith(marker):
            candidate = normalized_model[:-len(marker)]
            if candidate in TEXT_MODEL_ALIASES and TEXT_MODEL_ALIASES[candidate].thinking_model:
                base_model = candidate
                suffix_effort = suffix
            break

    route = TEXT_MODEL_ALIASES.get(base_model)
    resolved_effort = suffix_effort or normalize_text_thinking_effort(thinking_effort)
    if route is None:
        return requested_model, backend_thinking_effort(resolved_effort)

    backend_model = route.thinking_model if resolved_effort and route.thinking_model else route.backend_model
    return backend_model, backend_thinking_effort(resolved_effort)


def _model_item(model_id: str, source: dict[str, Any] | None = None) -> dict[str, Any]:
    source = source or {}
    return {
        "id": model_id,
        "object": "model",
        "created": int(source.get("created") or 0),
        "owned_by": "chatgpt2api" if model_id in TEXT_MODEL_ALIASES or model_id == "auto" else str(source.get("owned_by") or "chatgpt"),
        "permission": [],
        "root": model_id,
        "parent": None,
    }


def public_text_model_items(upstream_data: Iterable[object]) -> list[dict[str, Any]]:
    upstream = {
        str(item.get("id") or "").strip(): item
        for item in upstream_data
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    public = [_model_item("auto", upstream.get("auto"))]

    for alias, source_ids in _PUBLIC_MODEL_SOURCES.items():
        if not all(source_id in upstream for source_id in source_ids):
            continue
        public.append(_model_item(alias, upstream[source_ids[0]]))

    for model_id in _PASSTHROUGH_PUBLIC_MODELS:
        source = upstream.get(model_id)
        if source is not None:
            public.append(_model_item(model_id, source))

    return public
