from __future__ import annotations

import unittest
from unittest import mock

from services.config import config
from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from services.protocol import openai_v1_chat_complete, openai_v1_response
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    _image_model_fallback_chain,
    _is_image_model_quota_error,
    _validate_image_size_for_model,
    stream_image_outputs_with_pool,
)


class ImageModelRoutingTests(unittest.TestCase):
    def test_backend_model_slugs_come_from_live_config(self) -> None:
        routing = {
            "base_model": "gpt-5.6-sol",
            "thinking_model": "gpt-5.6-sol",
            "fallback_enabled": True,
        }
        backend = OpenAIBackendAPI()
        try:
            with mock.patch.dict(config.data, {"image_model_routing": routing}):
                self.assertEqual(backend._image_model_slug("gpt-image-2"), "gpt-5.6-sol")
                self.assertEqual(backend._image_model_slug("gpt-image-2-high"), "gpt-5.6-sol")
        finally:
            backend.close()

    def test_fallback_stays_inside_codex_transport_family(self) -> None:
        with mock.patch.dict(
            config.data,
            {"image_model_routing": {"base_model": "gpt-5-5", "thinking_model": "gpt-5-5-thinking", "fallback_enabled": True}},
        ):
            self.assertEqual(
                _image_model_fallback_chain("team-codex-gpt-image-2"),
                ["team-codex-gpt-image-2", "codex-gpt-image-2"],
            )
            self.assertEqual(_image_model_fallback_chain("gpt-image-2-high"), ["gpt-image-2-high"])
            self.assertEqual(
                _image_model_fallback_chain("team-codex-gpt-image-2", False),
                ["team-codex-gpt-image-2"],
            )

    def test_fallback_only_accepts_quota_or_rate_limit_errors(self) -> None:
        self.assertTrue(_is_image_model_quota_error(ImageGenerationError("quota", status_code=429)))
        self.assertTrue(_is_image_model_quota_error(RuntimeError("usage_limit_reached")))
        self.assertTrue(_is_image_model_quota_error(RuntimeError("no available team image quota")))
        self.assertFalse(_is_image_model_quota_error(RuntimeError("connection timed out")))
        self.assertFalse(_is_image_model_quota_error(RuntimeError("content policy rejection")))
        self.assertFalse(
            _is_image_model_quota_error(ImageGenerationError("content_policy", status_code=429))
        )
        self.assertFalse(
            _is_image_model_quota_error(ImageGenerationError(
                "请求被拦截",
                status_code=429,
                code="content_policy_violation",
            ))
        )
        self.assertFalse(
            _is_image_model_quota_error(ImageGenerationError("内容审核未通过", status_code=429))
        )

    def test_prepare_and_stream_use_one_backend_model_snapshot(self) -> None:
        first = {"base_model": "gpt-5.6-sol", "thinking_model": "gpt-5.6-sol", "fallback_enabled": True}
        second = {"base_model": "gpt-5-5", "thinking_model": "gpt-5-5-thinking", "fallback_enabled": True}
        backend = OpenAIBackendAPI(access_token="token")
        captured: list[tuple[str, str | None]] = []

        class FakeResponse:
            def close(self) -> None:
                pass

        def prepare(_prompt, _requirements, _model, *, backend_model="", thinking_effort=None):
            captured.append((backend_model, thinking_effort))
            config.data["image_model_routing"] = second
            return "conduit"

        def start(_prompt, _requirements, _conduit, _model, _references=None, *, backend_model="", thinking_effort=None):
            captured.append((backend_model, thinking_effort))
            return FakeResponse()

        try:
            with (
                mock.patch.dict(config.data, {"image_model_routing": first}),
                mock.patch.object(backend, "_bootstrap", return_value=None),
                mock.patch.object(backend, "_get_chat_requirements", return_value=ChatRequirements(token="requirements")),
                mock.patch.object(backend, "_prepare_image_conversation", side_effect=prepare),
                mock.patch.object(backend, "_start_image_generation", side_effect=start),
                mock.patch("services.openai_backend_api.iter_sse_payloads", return_value=iter(())),
            ):
                list(backend._stream_picture_conversation("cat", "gpt-image-2-high", []))
        finally:
            backend.close()

        self.assertEqual(captured, [("gpt-5.6-sol", "extended"), ("gpt-5.6-sol", "extended")])

    def test_high_resolution_sizes_are_codex_only(self) -> None:
        with self.assertRaises(ImageGenerationError) as context:
            list(stream_image_outputs_with_pool(ConversationRequest(
                model="gpt-image-2",
                prompt="cat",
                size="2048x2048",
            )))

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.code, "unsupported_image_size")
        _validate_image_size_for_model("codex-gpt-image-2", "3840x2160")

    def test_chat_image_protocol_forwards_size_quality_and_count(self) -> None:
        captured: list[ConversationRequest] = []

        def fake_stream(request: ConversationRequest):
            captured.append(request)
            for index in range(1, request.n + 1):
                yield ImageOutput(
                    kind="result",
                    model=request.model,
                    index=index,
                    total=request.n,
                    data=[{"b64_json": f"ZmFrZQ{index}="}],
                )

        with mock.patch.object(openai_v1_chat_complete, "stream_image_outputs_with_pool", side_effect=fake_stream):
            openai_v1_chat_complete.handle({
                "model": "gpt-image-2",
                "messages": [{"role": "user", "content": "cat"}],
                "n": 2,
                "size": "1024x1536",
                "quality": "high",
            })

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].n, 2)
        self.assertEqual(captured[0].size, "1024x1536")
        self.assertEqual(captured[0].quality, "high")

    def test_chat_image_protocol_rejects_web_high_resolution(self) -> None:
        with self.assertRaises(ImageGenerationError) as context:
            openai_v1_chat_complete.handle({
                "model": "gpt-image-2",
                "messages": [{"role": "user", "content": "cat"}],
                "size": "2048x2048",
            })

        self.assertEqual(context.exception.code, "unsupported_image_size")

    def test_responses_image_protocol_forwards_count(self) -> None:
        captured: list[ConversationRequest] = []

        def fake_stream(request: ConversationRequest):
            captured.append(request)
            for index in range(1, request.n + 1):
                yield ImageOutput(
                    kind="result",
                    model=request.model,
                    index=index,
                    total=request.n,
                    data=[{"b64_json": f"ZmFrZQ{index}="}],
                )

        with mock.patch.object(openai_v1_response, "stream_image_outputs_with_pool", side_effect=fake_stream):
            result = openai_v1_response.handle({
                "model": "gpt-image-2",
                "input": "cat",
                "n": 3,
                "tools": [{"type": "image_generation"}],
            })

        self.assertIsInstance(result, dict)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].n, 3)
        self.assertEqual(len(result["output"]), 3)


if __name__ == "__main__":
    unittest.main()
