from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from curl_cffi import CurlWsFlag

from services import openai_backend_api
from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest


class FakeSseResponse:
    headers: dict[str, str] = {}
    text = ""

    def __init__(
        self,
        payloads: list[str],
        *,
        websocket_url: str = "wss://example.test/socket",
        status_code: int = 200,
    ) -> None:
        self.payloads = payloads
        self.websocket_url = websocket_url
        self.status_code = status_code
        self.closed = False

    def iter_lines(self):
        for payload in self.payloads:
            yield f"data: {payload}".encode()

    def close(self) -> None:
        self.closed = True

    def json(self) -> dict:
        return {"websocket_url": self.websocket_url}


class FakeWebSocket:
    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, dict]] = []
        self.sent: list[str] = []
        self.closed = False
        self.connect_error: Exception | None = None

    def connect(self, url: str, **kwargs) -> None:
        self.connect_calls.append((url, kwargs))
        if self.connect_error is not None:
            raise self.connect_error

    def send_str(self, payload: str) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


class StreamHandoffTests(unittest.TestCase):
    def test_sol_text_stream_uses_model_discovery_account(self):
        created_tokens: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "") -> None:
                self.access_token = access_token
                created_tokens.append(access_token)

            def close(self) -> None:
                return None

        request = ConversationRequest(
            model="gpt-5.6-sol-wm",
            messages=[{"role": "user", "content": "hello"}],
        )
        with (
            mock.patch(
                "services.protocol.openai_v1_models.preferred_access_token_for_model",
                return_value="token-model-account",
            ),
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(
                conversation,
                "conversation_events",
                return_value=iter([{"type": "conversation.delta", "delta": "answer"}]),
            ),
            mock.patch.object(conversation.account_service, "mark_text_used"),
        ):
            deltas = list(
                conversation.stream_text_deltas(
                    SimpleNamespace(access_token="token-round-robin"),
                    request,
                )
            )

        self.assertEqual(created_tokens, ["token-model-account"])
        self.assertEqual(deltas, ["answer"])

    def test_gpt_56_sol_does_not_inject_generic_thinking_effort(self):
        backend = OpenAIBackendAPI(access_token="token-web-pro")
        try:
            payload = backend._conversation_payload(
                [{"role": "user", "content": "hello"}],
                "gpt-5.6-sol-wm",
                "Asia/Shanghai",
            )
        finally:
            backend.close()

        self.assertEqual(payload["model"], "gpt-5.6-sol-wm")
        self.assertNotIn("thinking_effort", payload)

    def test_stream_conversation_follows_handoff_before_emitting_done(self):
        handoff = json.dumps(
            {
                "type": "stream_handoff",
                "options": [
                    {
                        "type": "resume_sse_endpoint",
                        "topic_id": "conversation-turn-1",
                    },
                    {
                        "type": "subscribe_ws_topic",
                        "topic_id": "conversation-turn-1",
                    },
                ],
            }
        )
        response = FakeSseResponse(
            [
                json.dumps(
                    {
                        "type": "resume_conversation_token",
                        "kind": "topic",
                        "token": "resume-secret",
                        "conversation_id": "conversation-1",
                    }
                ),
                handoff,
                "[DONE]",
            ]
        )
        resumed = json.dumps(
            {
                "v": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["sol answer"]},
                    }
                }
            }
        )
        backend = OpenAIBackendAPI(access_token="token-web-pro")
        try:
            with (
                mock.patch.object(backend, "_bootstrap"),
                mock.patch.object(
                    backend,
                    "_get_chat_requirements",
                    return_value=ChatRequirements(token="requirements-token"),
                ),
                mock.patch.object(backend.session, "post", return_value=response),
                mock.patch.object(
                    backend,
                    "_stream_websocket_topic",
                    create=True,
                    return_value=iter([resumed, "[DONE]"]),
                ) as follow_handoff,
            ):
                payloads = list(
                    backend.stream_conversation(
                        messages=[{"role": "user", "content": "hello"}],
                        model="gpt-5.6-sol-wm",
                    )
                )
        finally:
            backend.close()

        self.assertTrue(response.closed)
        follow_handoff.assert_called_once_with("conversation-turn-1")
        self.assertEqual(payloads, [response.payloads[0], handoff, resumed, "[DONE]"])

    def test_stream_conversation_uses_resume_token_topic_without_handoff_event(self):
        claims = base64.urlsafe_b64encode(
            json.dumps({"turn_topic_id": "conversation-turn-jwt"}).encode()
        ).decode().rstrip("=")
        resume = json.dumps(
            {
                "type": "resume_conversation_token",
                "kind": "topic",
                "token": f"header.{claims}.signature",
                "conversation_id": "conversation-1",
            }
        )
        response = FakeSseResponse([resume, "[DONE]"])
        backend = OpenAIBackendAPI(access_token="token-web-pro")
        try:
            with (
                mock.patch.object(backend, "_bootstrap"),
                mock.patch.object(
                    backend,
                    "_get_chat_requirements",
                    return_value=ChatRequirements(token="requirements-token"),
                ),
                mock.patch.object(backend.session, "post", return_value=response),
                mock.patch.object(
                    backend,
                    "_stream_websocket_topic",
                    return_value=iter(["[DONE]"]),
                ) as follow_handoff,
            ):
                payloads = list(
                    backend.stream_conversation(
                        messages=[{"role": "user", "content": "hello"}],
                        model="gpt-5.6-sol-wm",
                    )
                )
        finally:
            backend.close()

        follow_handoff.assert_called_once_with("conversation-turn-jwt")
        self.assertEqual(payloads, [resume, "[DONE]"])

    def test_websocket_parser_reads_subscribe_catchups_and_ignores_other_topics(self):
        encoded = 'data: {"v":{"message":{"author":{"role":"assistant"}}}}\n\n'
        catchup = {
            "reply": {
                "topic_id": "conversation-turn-1",
                "catchups": [
                    {
                        "type": "message",
                        "topic_id": "conversation-turn-1",
                        "payload": {
                            "type": "conversation-turn-stream",
                            "payload": {"type": "stream-item", "encoded_item": encoded},
                        },
                    },
                    {
                        "type": "message",
                        "topic_id": "conversation-turn-other",
                        "payload": {
                            "type": "conversation-turn-stream",
                            "payload": {
                                "type": "stream-item",
                                "encoded_item": "data: should-not-pass\n\n",
                            },
                        },
                    },
                ],
            }
        }

        encoded_items, done = openai_backend_api._websocket_stream_items(
            catchup,
            "conversation-turn-1",
        )

        self.assertEqual(encoded_items, [encoded])
        self.assertFalse(done)

    def test_websocket_handoff_subscribes_and_yields_encoded_sse(self):
        response = FakeSseResponse([])
        resumed = json.dumps(
            {
                "v": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["sol answer"]},
                    }
                }
            }
        )
        websocket = FakeWebSocket()
        websocket_messages = [
            json.dumps([{"reply": {"type": "connect"}}]),
            json.dumps(
                [
                    {
                        "type": "message",
                        "topic_id": "conversation-turn-1",
                        "payload": {
                            "type": "conversation-turn-stream",
                            "payload": {
                                "type": "stream-item",
                                "encoded_item": f"data: {resumed}\n\n",
                            },
                        },
                    }
                ]
            ),
            json.dumps(
                [
                    {
                        "type": "message",
                        "topic_id": "conversation-turn-1",
                        "payload": {
                            "type": "conversation-turn-stream",
                            "payload": {"type": "done"},
                        },
                    }
                ]
            ),
        ]
        backend = OpenAIBackendAPI(access_token="token-web-pro")
        try:
            with (
                mock.patch.object(backend.session, "get", return_value=response) as get_url,
                mock.patch.object(openai_backend_api.requests, "WebSocket", return_value=websocket),
                mock.patch.object(
                    openai_backend_api,
                    "_recv_websocket_message",
                    side_effect=[(message.encode(), CurlWsFlag.TEXT) for message in websocket_messages],
                ),
            ):
                payloads = list(backend._stream_websocket_topic("conversation-turn-1"))
        finally:
            backend.close()

        self.assertEqual(payloads, [resumed, "[DONE]"])
        self.assertTrue(response.closed)
        self.assertTrue(websocket.closed)
        get_url.assert_called_once()
        self.assertEqual(get_url.call_args.args[0], "https://chatgpt.com/backend-api/celsius/ws/user")
        self.assertEqual(
            get_url.call_args.kwargs["headers"]["X-OpenAI-Target-Path"],
            "/backend-api/celsius/ws/user",
        )
        self.assertEqual(websocket.connect_calls[0][0], "wss://example.test/socket")
        self.assertEqual(
            websocket.connect_calls[0][1],
            {
                "timeout": 15.0,
                "headers": {"User-Agent": backend.user_agent, "Origin": "https://chatgpt.com"},
                "verify": True,
                "impersonate": backend.fp["impersonate"],
                "default_headers": False,
                "allow_redirects": False,
            },
        )
        command = json.loads(websocket.sent[0])
        self.assertEqual(command[0]["command"]["type"], "connect")
        self.assertEqual(
            command[1]["command"],
            {"type": "subscribe", "topic_id": "conversation-turn-1", "offset": "0"},
        )

    def test_websocket_handoff_preserves_https_proxy_url(self):
        response = FakeSseResponse([])
        websocket = FakeWebSocket()
        done = json.dumps(
            [
                {
                    "type": "message",
                    "topic_id": "conversation-turn-1",
                    "payload": {
                        "type": "conversation-turn-stream",
                        "payload": {"type": "done"},
                    },
                }
            ]
        )
        backend = OpenAIBackendAPI(access_token="token-web-pro")
        try:
            with (
                mock.patch.object(backend.session, "get", return_value=response),
                mock.patch.object(openai_backend_api.requests, "WebSocket", return_value=websocket),
                mock.patch.object(
                    openai_backend_api.proxy_settings,
                    "get_profile",
                    return_value=SimpleNamespace(
                        proxy_url="https://user:pass@proxy.example:8443",
                        skip_ssl_verify=False,
                    ),
                ),
                mock.patch.object(
                    openai_backend_api,
                    "_recv_websocket_message",
                    return_value=(done.encode(), CurlWsFlag.TEXT),
                ),
            ):
                self.assertEqual(
                    list(backend._stream_websocket_topic("conversation-turn-1")),
                    ["[DONE]"],
                )
        finally:
            backend.close()

        self.assertEqual(
            websocket.connect_calls[0][1]["proxy"],
            "https://user:pass@proxy.example:8443",
        )

    def test_websocket_handoff_rejects_insecure_url(self):
        response = FakeSseResponse([], websocket_url="ws://example.test/socket?secret=value")
        backend = OpenAIBackendAPI(access_token="token-web-pro")
        try:
            with (
                mock.patch.object(backend.session, "get", return_value=response),
                mock.patch.object(openai_backend_api.requests, "WebSocket") as websocket_class,
            ):
                with self.assertRaisesRegex(RuntimeError, "secure WebSocket URL"):
                    list(backend._stream_websocket_topic("conversation-turn-1"))
        finally:
            backend.close()

        self.assertTrue(response.closed)
        websocket_class.assert_not_called()

    def test_websocket_handoff_sanitizes_connection_error(self):
        response = FakeSseResponse([])
        websocket = FakeWebSocket()
        websocket.connect_error = RuntimeError("Set-Cookie: secret-value")
        backend = OpenAIBackendAPI(access_token="token-web-pro")
        try:
            with (
                mock.patch.object(backend.session, "get", return_value=response),
                mock.patch.object(openai_backend_api.requests, "WebSocket", return_value=websocket),
            ):
                with self.assertRaises(RuntimeError) as captured:
                    list(backend._stream_websocket_topic("conversation-turn-1"))
        finally:
            backend.close()

        self.assertNotIn("secret-value", str(captured.exception))
        self.assertIn("RuntimeError", str(captured.exception))
        self.assertTrue(websocket.closed)

    def test_websocket_handoff_retries_connection_once(self):
        response = FakeSseResponse([])
        failed_websocket = FakeWebSocket()
        failed_websocket.connect_error = RuntimeError("temporary failure")
        working_websocket = FakeWebSocket()
        done = json.dumps(
            [
                {
                    "type": "message",
                    "topic_id": "conversation-turn-1",
                    "payload": {
                        "type": "conversation-turn-stream",
                        "payload": {"type": "done"},
                    },
                }
            ]
        )
        backend = OpenAIBackendAPI(access_token="token-web-pro")
        try:
            with (
                mock.patch.object(backend.session, "get", return_value=response) as get_url,
                mock.patch.object(
                    openai_backend_api.requests,
                    "WebSocket",
                    side_effect=[failed_websocket, working_websocket],
                ),
                mock.patch.object(
                    openai_backend_api,
                    "_recv_websocket_message",
                    return_value=(done.encode(), CurlWsFlag.TEXT),
                ),
            ):
                self.assertEqual(
                    list(backend._stream_websocket_topic("conversation-turn-1")),
                    ["[DONE]"],
                )
        finally:
            backend.close()

        self.assertEqual(get_url.call_count, 2)
        self.assertTrue(failed_websocket.closed)
        self.assertTrue(working_websocket.closed)

    def test_websocket_handoff_close_frame_is_not_success(self):
        response = FakeSseResponse([])
        websocket = FakeWebSocket()
        backend = OpenAIBackendAPI(access_token="token-web-pro")
        try:
            with (
                mock.patch.object(backend.session, "get", return_value=response),
                mock.patch.object(openai_backend_api.requests, "WebSocket", return_value=websocket),
                mock.patch.object(
                    openai_backend_api,
                    "_recv_websocket_message",
                    return_value=(b"", CurlWsFlag.CLOSE),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "closed before completion"):
                    list(backend._stream_websocket_topic("conversation-turn-1"))
        finally:
            backend.close()

        self.assertTrue(websocket.closed)


if __name__ == "__main__":
    unittest.main()
