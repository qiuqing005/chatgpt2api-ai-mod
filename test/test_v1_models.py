from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import requests
from fastapi.testclient import TestClient

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from services.protocol import openai_v1_models
from services.config import config
from api import create_app


AUTH_KEY = "chatgpt2api"
BASE_URL = "http://localhost:8000"


class FakeResponse:
    status_code = 200
    text = ""
    headers: dict[str, str] = {}

    def __init__(self, body: dict | None = None) -> None:
        self.body = body or {}

    def json(self) -> dict:
        return self.body


class ModelListTests(unittest.TestCase):
    def setUp(self):
        openai_v1_models._clear_models_cache()

    def test_list_models_uses_authenticated_web_account(self):
        created_tokens: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                created_tokens.append(access_token)

            def list_models(self) -> dict:
                models = ["gpt-5.6-sol-wm"] if created_tokens[-1] else ["auto"]
                return {
                    "object": "list",
                    "data": [{"id": model} for model in models],
                }

            def close(self) -> None:
                return None

        with (
            mock.patch.object(openai_v1_models, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(
                openai_v1_models.account_service,
                "list_accounts",
                return_value=[
                    {
                        "access_token": "token-web-pro",
                        "type": "Pro",
                        "source_type": "web",
                        "status": "正常",
                    },
                ],
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "refresh_access_token",
                return_value="token-web-pro",
            ),
        ):
            result = openai_v1_models.list_models()

        self.assertEqual(created_tokens, ["token-web-pro"])
        ids = {item["id"] for item in result["data"]}
        self.assertIn("gpt-5.6-sol", ids)
        self.assertNotIn("gpt-5.6", ids)
        self.assertNotIn("gpt-5.6-sol-wm", ids)

    def test_list_models_falls_back_to_anonymous_when_web_account_fails(self):
        created_tokens: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token
                created_tokens.append(access_token)

            def list_models(self) -> dict:
                if self.access_token:
                    raise RuntimeError("authenticated models unavailable")
                return {"object": "list", "data": [{"id": "auto"}]}

            def close(self) -> None:
                return None

        with (
            mock.patch.object(openai_v1_models, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(
                openai_v1_models.account_service,
                "list_accounts",
                return_value=[
                    {
                        "access_token": "token-web-pro",
                        "type": "Pro",
                        "source_type": "web",
                        "status": "正常",
                    },
                ],
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "refresh_access_token",
                return_value="token-web-pro",
            ),
        ):
            result = openai_v1_models.list_models()
            second = openai_v1_models.list_models()

        self.assertEqual(created_tokens, ["token-web-pro", ""])
        self.assertEqual([item["id"] for item in result["data"]], ["auto", "gpt-image-2"])
        self.assertEqual([item["id"] for item in second["data"]], ["auto", "gpt-image-2"])

    def test_list_models_falls_back_when_token_refresh_fails(self):
        created_tokens: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                created_tokens.append(access_token)

            def list_models(self) -> dict:
                return {"object": "list", "data": [{"id": "auto"}]}

            def close(self) -> None:
                return None

        with (
            mock.patch.object(openai_v1_models, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(
                openai_v1_models.account_service,
                "list_accounts",
                return_value=[
                    {
                        "access_token": "token-web-pro",
                        "type": "Pro",
                        "source_type": "web",
                        "status": "正常",
                    },
                ],
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "refresh_access_token",
                side_effect=RuntimeError("refresh failed"),
            ),
        ):
            result = openai_v1_models.list_models()

        self.assertEqual(created_tokens, [""])
        self.assertEqual([item["id"] for item in result["data"]], ["auto", "gpt-image-2"])

    def test_list_models_reuses_authenticated_cache(self):
        created_tokens: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                created_tokens.append(access_token)

            def list_models(self) -> dict:
                return {"object": "list", "data": [{"id": "gpt-5.6-sol-wm"}]}

            def close(self) -> None:
                return None

        account = {
            "access_token": "token-web-pro",
            "type": "Pro",
            "source_type": "web",
            "status": "正常",
        }
        with (
            mock.patch.object(openai_v1_models, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=[account]),
            mock.patch.object(
                openai_v1_models.account_service,
                "refresh_access_token",
                return_value="token-web-pro",
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "resolve_access_token",
                return_value="token-web-pro",
            ),
            mock.patch.object(openai_v1_models.account_service, "get_account", return_value=account),
        ):
            first = openai_v1_models.list_models()
            first["data"].clear()
            second = openai_v1_models.list_models()
            preferred = openai_v1_models.preferred_access_token_for_model("gpt-5.6-sol-wm")

        self.assertEqual(created_tokens, ["token-web-pro"])
        self.assertIn("gpt-5.6-sol", {item["id"] for item in second["data"]})
        self.assertEqual(preferred, "token-web-pro")

    def test_model_discovery_merges_accounts_and_keeps_per_model_tokens(self):
        created_tokens: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token
                created_tokens.append(access_token)

            def list_models(self) -> dict:
                models = {
                    "token-luna": ["gpt-5.6-luna-wm", "gpt-5-5"],
                    "token-terra-sol": ["gpt-5.6-terra-wm", "gpt-5.6-sol-wm", "gpt-5-5"],
                    "token-unused": ["gpt-5-4-thinking"],
                }[self.access_token]
                return {"object": "list", "data": [{"id": model} for model in models]}

            def close(self) -> None:
                return None

        accounts = [
            {"access_token": "token-luna", "type": "Pro", "source_type": "web", "status": "正常"},
            {"access_token": "token-terra-sol", "type": "Pro", "source_type": "web", "status": "正常"},
            {"access_token": "token-unused", "type": "Pro", "source_type": "web", "status": "正常"},
        ]

        with (
            mock.patch.object(openai_v1_models, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=accounts),
            mock.patch.object(
                openai_v1_models.account_service,
                "refresh_access_token",
                side_effect=lambda token, **_kwargs: token,
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "resolve_access_token",
                side_effect=lambda token: token,
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "get_account",
                side_effect=lambda token: next(account for account in accounts if account["access_token"] == token),
            ),
        ):
            luna_token = openai_v1_models.preferred_access_token_for_model("gpt-5.6-luna-wm")
            result = openai_v1_models.list_models()
            terra_token = openai_v1_models.preferred_access_token_for_model("gpt-5.6-terra-wm")
            sol_token = openai_v1_models.preferred_access_token_for_model("gpt-5.6-sol-wm")

        ids = {item["id"] for item in result["data"]}
        self.assertEqual(created_tokens, ["token-luna", "token-terra-sol", "token-unused"])
        self.assertTrue({"gpt-5.4", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"} <= ids)
        self.assertEqual(luna_token, "token-luna")
        self.assertEqual(terra_token, "token-terra-sol")
        self.assertEqual(sol_token, "token-terra-sol")

    def test_model_specific_tokens_rotate_and_respect_exclusions(self):
        accounts = [
            {"access_token": "token-a", "type": "Pro", "source_type": "web", "status": "正常"},
            {"access_token": "token-b", "type": "Pro", "source_type": "web", "status": "正常"},
        ]

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token

            def list_models(self) -> dict:
                return {"object": "list", "data": [{"id": "gpt-5-5-thinking"}]}

            def close(self) -> None:
                return None

        with (
            mock.patch.object(openai_v1_models, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=accounts),
            mock.patch.object(
                openai_v1_models.account_service,
                "refresh_access_token",
                side_effect=lambda token, **_kwargs: token,
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "resolve_access_token",
                side_effect=lambda token: token,
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "get_account",
                side_effect=lambda token: next(account for account in accounts if account["access_token"] == token),
            ),
        ):
            first = openai_v1_models.preferred_access_token_for_model("gpt-5-5-thinking")
            second = openai_v1_models.preferred_access_token_for_model("gpt-5-5-thinking")
            fallback = openai_v1_models.preferred_access_token_for_model(
                "gpt-5-5-thinking",
                {"token-a"},
            )

        self.assertEqual((first, second), ("token-a", "token-b"))
        self.assertEqual(fallback, "token-b")

    def test_partial_refresh_keeps_complete_stale_model_snapshot(self):
        phase = {"value": "healthy"}

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token

            def list_models(self) -> dict:
                if phase["value"] == "partial" and self.access_token == "token-luna":
                    raise RuntimeError("temporary discovery failure")
                model = "gpt-5.6-luna-wm" if self.access_token == "token-luna" else "gpt-5.6-terra-wm"
                return {"object": "list", "data": [{"id": model}]}

            def close(self) -> None:
                return None

        accounts = [
            {"access_token": "token-luna", "type": "Pro", "source_type": "web", "status": "正常"},
            {"access_token": "token-terra", "type": "Pro", "source_type": "web", "status": "正常"},
        ]

        with (
            mock.patch.object(openai_v1_models, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=accounts),
            mock.patch.object(
                openai_v1_models.account_service,
                "refresh_access_token",
                side_effect=lambda token, **_kwargs: token,
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "resolve_access_token",
                side_effect=lambda token: token,
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "get_account",
                side_effect=lambda token: next(account for account in accounts if account["access_token"] == token),
            ),
        ):
            first = openai_v1_models.list_models()
            with openai_v1_models._models_cache_lock:
                _timestamp, cached = openai_v1_models._models_cache["auth"]
                openai_v1_models._models_cache["auth"] = (
                    openai_v1_models.time.monotonic() - openai_v1_models._MODELS_CACHE_TTL_SECS - 1,
                    cached,
                )
            phase["value"] = "partial"
            second = openai_v1_models.list_models()
            luna_token = openai_v1_models.preferred_access_token_for_model("gpt-5.6-luna-wm")

        expected = {"gpt-5.6-luna", "gpt-5.6-terra"}
        self.assertTrue(expected <= {item["id"] for item in first["data"]})
        self.assertTrue(expected <= {item["id"] for item in second["data"]})
        self.assertEqual(luna_token, "token-luna")

    def test_stale_model_tokens_are_served_while_background_refresh_starts(self):
        account = {"access_token": "token-sol", "type": "Pro", "source_type": "web", "status": "正常"}
        with openai_v1_models._models_cache_lock:
            openai_v1_models._models_cache["auth"] = (
                openai_v1_models.time.monotonic() - openai_v1_models._MODELS_CACHE_TTL_SECS - 1,
                {"object": "list", "data": [{"id": "gpt-5.6-sol-wm"}]},
            )
            openai_v1_models._models_auth_tokens["gpt-5.6-sol-wm"] = ["token-sol"]

        with (
            mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=[account]),
            mock.patch.object(openai_v1_models.account_service, "resolve_access_token", return_value="token-sol"),
            mock.patch.object(openai_v1_models.account_service, "get_account", return_value=account),
            mock.patch.object(openai_v1_models, "_refresh_models_in_background") as refresh,
        ):
            token = openai_v1_models.preferred_access_token_for_model("gpt-5.6-sol-wm")

        self.assertEqual(token, "token-sol")
        refresh.assert_called_once()

    def test_list_models_collapses_web_slugs_into_stable_aliases(self):
        upstream_models = [
            "gpt-5-3",
            "gpt-5-3-instant",
            "gpt-5-4-thinking",
            "gpt-5-4-pro",
            "gpt-5-5",
            "gpt-5-5-thinking",
            "gpt-5.5-wm",
            "gpt-5.5-cca-wm",
            "gpt-5.6-luna-wm",
            "gpt-5.6-terra-wm",
            "gpt-5.6-sol-wm",
            "gpt-5-6-thinking",
            "gpt-5-5-mini",
            "gpt-5-6-pro",
            "o3",
            "o3-pro",
            "research",
        ]

        with (
            mock.patch.object(
                openai_v1_models.OpenAIBackendAPI,
                "list_models",
                return_value={"object": "list", "data": [{"id": model} for model in upstream_models]},
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "list_accounts",
                return_value=[{"access_token": "token-web-pro", "type": "Pro", "source_type": "web"}],
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "refresh_access_token",
                return_value="token-web-pro",
            ),
        ):
            result = openai_v1_models.list_models()

        ids = [item["id"] for item in result["data"]]
        self.assertEqual(
            ids,
            [
                "auto",
                "gpt-5.3",
                "gpt-5.4",
                "gpt-5.5",
                "gpt-5.6-luna",
                "gpt-5.6-terra",
                "gpt-5.6-sol",
                "gpt-5-mini",
                "gpt-5-pro",
                "o3",
                "o3-pro",
                "research",
                "gpt-image-2",
            ],
        )
        self.assertFalse(any(model.endswith(("-low", "-medium", "-high", "-xhigh")) for model in ids))
        self.assertFalse(any("-wm" in model or "thinking" in model for model in ids))
        self.assertNotIn("gpt-5.6", ids)

    def test_gpt_55_is_not_public_without_its_thinking_backend(self):
        with (
            mock.patch.object(
                openai_v1_models.OpenAIBackendAPI,
                "list_models",
                return_value={"object": "list", "data": [{"id": "gpt-5-5"}]},
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "list_accounts",
                return_value=[{"access_token": "token-web-pro", "type": "Pro", "source_type": "web"}],
            ),
            mock.patch.object(
                openai_v1_models.account_service,
                "refresh_access_token",
                return_value="token-web-pro",
            ),
        ):
            result = openai_v1_models.list_models()

        self.assertNotIn("gpt-5.5", {item["id"] for item in result["data"]})

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

    def test_gpt_image_hidden_suffixes_send_thinking_effort_payload(self):
        backend = OpenAIBackendAPI()
        payloads: list[dict] = []

        def fake_post(*args, **kwargs):
            payloads.append(kwargs["json"])
            if str(args[0]).endswith("/backend-api/f/conversation/prepare"):
                return FakeResponse({"conduit_token": "ct-1"})
            return FakeResponse()

        try:
            with mock.patch.object(backend.session, "post", side_effect=fake_post):
                requirements = ChatRequirements(token="requirements-token")
                self.assertEqual(
                    backend._prepare_image_conversation("draw", requirements, "gpt-image-2-high"),
                    "ct-1",
                )
                backend._start_image_generation("draw", requirements, "ct-1", "gpt-image-2-high")
        finally:
            backend.close()

        self.assertEqual(payloads[0]["model"], "gpt-5-5-thinking")
        self.assertEqual(payloads[0]["thinking_effort"], "extended")
        self.assertEqual(payloads[1]["model"], "gpt-5-5-thinking")
        self.assertEqual(payloads[1]["thinking_effort"], "extended")

    def test_list_models_function(self):
        """测试直接调用服务层获取模型列表。"""
        if os.getenv("CHATGPT2API_INTEGRATION_TESTS") != "1":
            self.skipTest("set CHATGPT2API_INTEGRATION_TESTS=1 to run online model-list test")
        result = openai_v1_models.list_models()
        print("function result:")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    def test_list_models_http_contract_returns_projected_service_result(self):
        projected = {
            "object": "list",
            "data": [
                {"id": "auto", "object": "model"},
                {"id": "gpt-5.6-luna", "object": "model"},
                {"id": "gpt-5.6-terra", "object": "model"},
                {"id": "gpt-5.6-sol", "object": "model"},
            ],
        }

        with (
            mock.patch.object(openai_v1_models, "list_models", return_value=projected),
            TestClient(create_app()) as client,
        ):
            response = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {config.auth_key}"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), projected)

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
