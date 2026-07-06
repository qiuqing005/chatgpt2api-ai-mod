from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import requests

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import openai_v1_models


AUTH_KEY = "chatgpt2api"
BASE_URL = "http://localhost:8000"


class ModelListTests(unittest.TestCase):
    def test_list_models_only_returns_image_models_backed_by_account_types(self):
        with (
            mock.patch.object(
                openai_v1_models.OpenAIBackendAPI,
                "list_models",
                return_value={"object": "list", "data": []},
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "list_accounts",
                return_value=[
                    {"access_token": "token-free", "type": "free"},
                    {"access_token": "token-web-team", "type": "Team", "source_type": "web"},
                    {"access_token": "token-codex-team", "type": "Team", "source_type": "codex"},
                ],
            ),
        ):
            result = openai_v1_models.list_models()

        ids = {item["id"] for item in result["data"]}
        self.assertIn("gpt-image-2", ids)
        self.assertIn("codex-gpt-image-2", ids)
        self.assertIn("team-codex-gpt-image-2", ids)
        self.assertNotIn("plus-codex-gpt-image-2", ids)
        self.assertNotIn("pro-codex-gpt-image-2", ids)

    def test_list_models_does_not_return_codex_models_for_web_plus_accounts(self):
        with (
            mock.patch.object(
                openai_v1_models.OpenAIBackendAPI,
                "list_models",
                return_value={"object": "list", "data": []},
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "list_accounts",
                return_value=[
                    {"access_token": "token-web-plus", "type": "Plus", "source_type": "web"},
                ],
            ),
        ):
            result = openai_v1_models.list_models()

        ids = {item["id"] for item in result["data"]}
        self.assertIn("gpt-image-2", ids)
        self.assertNotIn("codex-gpt-image-2", ids)
        self.assertNotIn("plus-codex-gpt-image-2", ids)
        self.assertNotIn("gpt-image-2-low", ids)
        self.assertNotIn("gpt-image-2-medium", ids)
        self.assertNotIn("gpt-image-2-high", ids)
        self.assertNotIn("gpt-image-2-xhigh", ids)

    def test_gpt_image_hidden_suffixes_select_thinking_backend_slug(self):
        backend = OpenAIBackendAPI()
        try:
            self.assertEqual(backend._image_model_slug("gpt-image-2"), "gpt-5-5")
            self.assertEqual(backend._image_model_slug("gpt-image-2-low"), "gpt-5-5-thinking")
            self.assertEqual(backend._image_model_slug("gpt-image-2-medium"), "gpt-5-5-thinking")
            self.assertEqual(backend._image_model_slug("gpt-image-2-high"), "gpt-5-5-thinking")
            self.assertEqual(backend._image_model_slug("gpt-image-2-xhigh"), "gpt-5-5-thinking")
        finally:
            backend.close()

    def test_list_models_function(self):
        """测试直接调用服务层获取模型列表。"""
        if os.getenv("CHATGPT2API_INTEGRATION_TESTS") != "1":
            self.skipTest("set CHATGPT2API_INTEGRATION_TESTS=1 to run online model-list test")
        result = openai_v1_models.list_models()
        print("function result:")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    def test_list_models_http(self):
        """测试通过 HTTP 接口获取模型列表。"""
        if os.getenv("CHATGPT2API_INTEGRATION_TESTS") != "1":
            self.skipTest("set CHATGPT2API_INTEGRATION_TESTS=1 to run HTTP model-list test")
        response = requests.get(
            f"{BASE_URL}/v1/models",
            headers={"Authorization": f"Bearer {AUTH_KEY}"},
            timeout=30,
        )
        print("http status:")
        print(response.status_code)
        print("http result:")
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
