from __future__ import annotations

from unittest import mock

from services.config import config
from services.protocol import openai_v1_models


def test_visible_models_allowlist_filters_defaults_and_adds_custom_model():
    original = config.data.get("visible_models", None)
    try:
        config.data["visible_models"] = ["gpt-5.5", "custom-model"]
        with mock.patch.object(
            openai_v1_models,
            "_load_upstream_models",
            return_value={
                "object": "list",
                "data": [
                    {"id": "auto"},
                    {"id": "gpt-5-5"},
                    {"id": "gpt-5-5-thinking"},
                    {"id": "gpt-5-6-thinking"},
                ],
            },
        ), mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=[]):
            result = openai_v1_models.list_models()

        assert [item["id"] for item in result["data"]] == ["gpt-5.5", "custom-model"]
    finally:
        if original is None:
            config.data.pop("visible_models", None)
        else:
            config.data["visible_models"] = original


def test_visible_models_null_keeps_default_public_filtering():
    original = config.data.get("visible_models", None)
    try:
        config.data["visible_models"] = None
        with mock.patch.object(
            openai_v1_models,
            "_load_upstream_models",
            return_value={
                "object": "list",
                "data": [
                    {"id": "auto"},
                    {"id": "gpt-5-5"},
                    {"id": "gpt-5-5-thinking"},
                ],
            },
        ), mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=[]):
            result = openai_v1_models.list_models()

        assert [item["id"] for item in result["data"]] == ["auto", "gpt-5.5"]
    finally:
        if original is None:
            config.data.pop("visible_models", None)
        else:
            config.data["visible_models"] = original
