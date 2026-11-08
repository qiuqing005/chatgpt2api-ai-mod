from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest
from services.protocol.text_model_aliases import (
    backend_thinking_effort,
    resolve_text_backend_route,
)


class TextModelAliasTests(unittest.TestCase):
    def test_hidden_suffixes_map_to_backend_efforts(self) -> None:
        expected = {
            "gpt-6.1-sol-low": ("gpt-6.1-sol-wm", "min"),
            "gpt-6.1-sol-medium": ("gpt-6.1-sol-wm", "standard"),
            "gpt-6.1-sol-high": ("gpt-6.1-sol-wm", "extended"),
            "gpt-6.1-sol-xhigh": ("gpt-6.1-sol-wm", "max"),
        }
        for model, route in expected.items():
            with self.subTest(model=model):
                self.assertEqual(resolve_text_backend_route(model), route)

    def test_suffix_overrides_explicit_effort(self) -> None:
        self.assertEqual(
            resolve_text_backend_route("gpt-6.1-sol-low", "xhigh"),
            ("gpt-6.1-sol-wm", "min"),
        )

    def test_explicit_effort_is_applied_to_sol_backend(self) -> None:
        self.assertEqual(resolve_text_backend_route("gpt-6.1-sol"), ("gpt-6.1-sol-wm", ""))
        self.assertEqual(
            resolve_text_backend_route("gpt-6.1-sol", "high"),
            ("gpt-6.1-sol-wm", "extended"),
        )

    def test_raw_models_are_preserved_but_effort_is_normalized(self) -> None:
        self.assertEqual(
            resolve_text_backend_route("gpt-6.1-sol-wm", "xhigh"),
            ("gpt-6.1-sol-wm", "max"),
        )
        self.assertEqual(backend_thinking_effort("standard"), "standard")

    def test_independent_gpt_61_variants_route_to_their_own_backend(self) -> None:
        self.assertEqual(
            resolve_text_backend_route("gpt-6.1-sol-high"),
            ("gpt-6.1-sol-wm", "extended"),
        )
        self.assertEqual(
            resolve_text_backend_route("gpt-6.1-terra-xhigh"),
            ("gpt-6.1-terra-wm", "max"),
        )
        self.assertEqual(
            resolve_text_backend_route("gpt-6.1-sol-low"),
            ("gpt-6.1-sol-wm", "min"),
        )

    def test_stream_routes_alias_before_selecting_discovery_account(self) -> None:
        created_tokens: list[str] = []
        captured: list[tuple[str, str]] = []

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token
                created_tokens.append(access_token)

            def close(self) -> None:
                return None

        def fake_events(_backend, **kwargs):
            captured.append((kwargs["model"], kwargs["thinking_effort"]))
            return iter([{"type": "conversation.delta", "delta": "ok"}])

        with (
            mock.patch(
                "services.protocol.openai_v1_models.preferred_access_token_for_model",
                return_value="token-model-account",
            ),
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(conversation, "conversation_events", side_effect=fake_events),
            mock.patch.object(conversation.account_service, "mark_text_used"),
        ):
            result = list(conversation.stream_text_deltas(
                SimpleNamespace(access_token="token-round-robin"),
                ConversationRequest(
                    model="gpt-6.1-sol-xhigh",
                    messages=[{"role": "user", "content": "hello"}],
                ),
            ))

        self.assertEqual(result, ["ok"])
        self.assertEqual(created_tokens, ["token-model-account"])
        self.assertEqual(captured, [("gpt-6.1-sol-wm", "max")])

    def test_regular_text_alias_uses_a_compatible_discovery_account(self) -> None:
        created_tokens: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token
                created_tokens.append(access_token)

            def close(self) -> None:
                return None

        with (
            mock.patch(
                "services.protocol.openai_v1_models.preferred_access_token_for_model",
                return_value="token-model-account",
            ),
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(
                conversation,
                "conversation_events",
                return_value=iter([{"type": "conversation.delta", "delta": "ok"}]),
            ),
            mock.patch.object(conversation.account_service, "mark_text_used"),
        ):
            result = list(conversation.stream_text_deltas(
                SimpleNamespace(access_token="token-round-robin"),
                ConversationRequest(
                    model="gpt-6.1-sol-high",
                    messages=[{"role": "user", "content": "hello"}],
                ),
            ))

        self.assertEqual(result, ["ok"])
        self.assertEqual(created_tokens, ["token-model-account"])

    def test_auto_model_keeps_normal_account_rotation(self) -> None:
        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token

            def close(self) -> None:
                return None

        with (
            mock.patch(
                "services.protocol.openai_v1_models.preferred_access_token_for_model",
                return_value="unexpected-token",
            ) as preferred,
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(
                conversation,
                "conversation_events",
                return_value=iter([{"type": "conversation.delta", "delta": "ok"}]),
            ),
            mock.patch.object(conversation.account_service, "mark_text_used"),
        ):
            result = list(conversation.stream_text_deltas(
                SimpleNamespace(access_token="token-round-robin"),
                ConversationRequest(
                    model="auto",
                    messages=[{"role": "user", "content": "hello"}],
                ),
            ))

        self.assertEqual(result, ["ok"])
        preferred.assert_not_called()

    def test_model_specific_route_retries_next_compatible_account_before_output(self) -> None:
        created_tokens: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token
                created_tokens.append(access_token)

            def close(self) -> None:
                return None

        calls = 0

        def fake_events(_backend, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("model temporarily unavailable")
            return iter([{"type": "conversation.delta", "delta": "ok"}])

        with (
            mock.patch(
                "services.protocol.openai_v1_models.preferred_access_token_for_model",
                side_effect=["token-a", "token-b"],
            ),
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(conversation, "conversation_events", side_effect=fake_events),
            mock.patch.object(conversation.account_service, "mark_text_used"),
        ):
            result = list(conversation.stream_text_deltas(
                SimpleNamespace(access_token="token-round-robin"),
                ConversationRequest(
                    model="gpt-6.1-terra-high",
                    messages=[{"role": "user", "content": "hello"}],
                ),
            ))

        self.assertEqual(result, ["ok"])
        self.assertEqual(created_tokens, ["token-a", "token-b"])

    def test_invalid_model_token_does_not_fall_back_to_incompatible_pool(self) -> None:
        created_tokens: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token
                created_tokens.append(access_token)

            def close(self) -> None:
                return None

        calls = 0

        def fake_events(_backend, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("token invalidated")
            return iter([{"type": "conversation.delta", "delta": "ok"}])

        with (
            mock.patch(
                "services.protocol.openai_v1_models.preferred_access_token_for_model",
                side_effect=["token-a", "token-b"],
            ),
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(conversation, "conversation_events", side_effect=fake_events),
            mock.patch.object(conversation.account_service, "refresh_access_token", return_value=""),
            mock.patch.object(conversation.account_service, "remove_invalid_token"),
            mock.patch.object(
                conversation.account_service,
                "get_text_access_token",
                side_effect=AssertionError("explicit models must not use the generic pool"),
            ),
            mock.patch.object(conversation.account_service, "mark_text_used"),
        ):
            result = list(conversation.stream_text_deltas(
                SimpleNamespace(access_token="token-round-robin"),
                ConversationRequest(
                    model="gpt-6.1-sol-low",
                    messages=[{"role": "user", "content": "hello"}],
                ),
            ))

        self.assertEqual(result, ["ok"])
        self.assertEqual(created_tokens, ["token-a", "token-b"])

    def test_conversation_payload_uses_real_web_effort_values(self) -> None:
        backend = OpenAIBackendAPI(access_token="token-web-pro")
        try:
            payload = backend._conversation_payload(
                [{"role": "user", "content": "hello"}],
                "gpt-6.1-sol-wm",
                "Asia/Shanghai",
                thinking_effort="xhigh",
            )
        finally:
            backend.close()

        self.assertEqual(payload["thinking_effort"], "max")


if __name__ == "__main__":
    unittest.main()
