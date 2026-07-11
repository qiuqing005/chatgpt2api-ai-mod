from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.system as system_module
from services.config import ConfigStore


AUTH_HEADERS = {"Authorization": "Bearer admin"}


class FakeConfig:
    def __init__(self) -> None:
        self.settings = {
            "base_model": "gpt-5-5",
            "thinking_model": "gpt-5-5-thinking",
            "fallback_enabled": True,
            "task_workers": 2,
        }

    def get_image_generation_settings(self) -> dict:
        return dict(self.settings)

    def update_image_generation_settings(self, value: dict) -> dict:
        self.settings = dict(value)
        return dict(self.settings)

    def get(self) -> dict:
        return {}

    def update(self, _value: dict) -> dict:
        return {}


class ImageGenerationSettingsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake_config = FakeConfig()
        self.require_admin = mock.Mock(return_value={"id": "admin", "role": "admin"})
        patchers = [
            mock.patch.object(system_module, "config", self.fake_config),
            mock.patch.object(system_module, "require_admin", self.require_admin),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(system_module.create_router("test"))
        self.client = TestClient(app)

    def test_get_returns_current_and_default_backend_options_without_discovery(self) -> None:
        response = self.client.get("/api/settings/image-generation", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("gpt-5-5", response.json()["model_options"])
        self.assertIn("gpt-5-5-thinking", response.json()["model_options"])
        self.assertNotIn("gpt-image-2", response.json()["model_options"])

    def test_patch_is_strict_and_updates_only_image_settings(self) -> None:
        payload = {
            "base_model": "gpt-5.6-sol",
            "thinking_model": "gpt-5.6-sol",
            "fallback_enabled": False,
            "task_workers": 3,
        }
        response = self.client.patch("/api/settings/image-generation", headers=AUTH_HEADERS, json=payload)
        invalid = self.client.patch(
            "/api/settings/image-generation",
            headers=AUTH_HEADERS,
            json={**payload, "unexpected": True},
        )
        partial = self.client.patch(
            "/api/settings/image-generation",
            headers=AUTH_HEADERS,
            json={"base_model": "gpt-5.6-sol", "thinking_model": "gpt-5.6-sol"},
        )
        coerced = self.client.patch(
            "/api/settings/image-generation",
            headers=AUTH_HEADERS,
            json={**payload, "fallback_enabled": "false", "task_workers": "3"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["settings"], payload)
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(partial.status_code, 422, partial.text)
        self.assertEqual(coerced.status_code, 422, coerced.text)

    def test_requires_admin(self) -> None:
        from fastapi import HTTPException

        self.require_admin.side_effect = HTTPException(status_code=403, detail={"error": "admin required"})
        response = self.client.get("/api/settings/image-generation", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 403, response.text)

    def test_legacy_http_settings_cannot_overwrite_image_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.json"
            path.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = ConfigStore(path)
            expected = store.update_image_generation_settings({
                "base_model": "gpt-5.6-sol",
                "thinking_model": "gpt-5.6-sol",
                "fallback_enabled": False,
                "task_workers": 4,
            })
            with mock.patch.object(system_module, "config", store):
                response = self.client.post(
                    "/api/settings",
                    headers=AUTH_HEADERS,
                    json={
                        "image_model_routing": {"base_model": "gpt-5-5"},
                        "image_task_workers": 1,
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(store.get_image_generation_settings(), expected)


if __name__ == "__main__":
    unittest.main()
