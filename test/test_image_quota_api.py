from __future__ import annotations

import base64
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.ai as ai_module
from services.auth_service import ImageQuotaExceeded


AUTH_HEADERS = {"Authorization": "Bearer user-key"}
IDENTITY = {"id": "user-1", "name": "User", "role": "user", "image_quota": 10, "image_used": 0}


class ImageQuotaApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.quota = mock.Mock()
        self.quota.reserve_image_quota.return_value = True
        self.quota.settle_image_quota.return_value = True
        patchers = [
            mock.patch.object(ai_module, "auth_service", self.quota),
            mock.patch.object(ai_module, "require_identity", return_value=IDENTITY),
            mock.patch.object(ai_module, "check_request", return_value=None),
            mock.patch.object(ai_module.LoggedCall, "log", return_value=None),
            mock.patch.object(ai_module.openai_v1_image_generations, "handle", return_value={"data": [{"url": "x"}]}),
            mock.patch.object(ai_module.openai_v1_image_edit, "handle", return_value={"data": [{"url": "edited"}]}),
            mock.patch.object(ai_module.openai_v1_chat_complete, "handle", return_value={"choices": []}),
            mock.patch.object(ai_module.openai_v1_response, "handle", return_value={"output": []}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)

    def test_all_public_image_protocols_reserve_quota(self) -> None:
        generation = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={"prompt": "cat", "model": "gpt-image-2", "n": 4},
        )
        chat = self.client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={"model": "gpt-image-2-high", "n": 2, "messages": [{"role": "user", "content": "cat"}]},
        )
        responses = self.client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={"model": "gpt-image-2", "n": 3, "input": "cat", "tools": [{"type": "image_generation"}]},
        )
        image_url = "data:image/png;base64," + base64.b64encode(b"png").decode("ascii")
        edit = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={"model": "gpt-image-2", "prompt": "edit", "n": 2, "image": image_url},
        )

        self.assertEqual(
            [generation.status_code, chat.status_code, responses.status_code, edit.status_code],
            [200, 200, 200, 200],
        )
        self.assertEqual(
            [call.args[1] for call in self.quota.reserve_image_quota.call_args_list],
            [4, 2, 3, 2],
        )
        self.assertTrue(all(call.kwargs["reservation_id"].startswith("api:") for call in self.quota.reserve_image_quota.call_args_list))

    def test_text_chat_does_not_consume_image_quota(self) -> None:
        response = self.client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.quota.reserve_image_quota.assert_not_called()

    def test_quota_exhaustion_returns_429(self) -> None:
        self.quota.reserve_image_quota.side_effect = ImageQuotaExceeded("图片生成额度不足")

        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={"prompt": "cat", "model": "gpt-image-2", "n": 1},
        )

        self.assertEqual(response.status_code, 429, response.text)
        self.assertIn("图片生成额度不足", response.text)

    def test_partial_non_stream_result_refunds_missing_images(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={"prompt": "cat", "model": "gpt-image-2", "n": 3},
        )

        self.assertEqual(response.status_code, 200, response.text)
        settlement = self.quota.settle_image_quota.call_args
        self.assertEqual(settlement.args, ("user-1", 1))
        self.assertTrue(settlement.kwargs["reservation_id"].startswith("api:"))

    def test_partial_stream_result_refunds_missing_images(self) -> None:
        ai_module.openai_v1_image_generations.handle.return_value = iter(
            [{"object": "image.generation.result", "data": [{"url": "stream-image"}]}]
        )

        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={"prompt": "cat", "model": "gpt-image-2", "n": 2, "stream": True},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("stream-image", response.text)
        settlement = self.quota.settle_image_quota.call_args
        self.assertEqual(settlement.args, ("user-1", 1))
        self.assertTrue(settlement.kwargs["reservation_id"].startswith("api:"))

    def test_stream_settlement_retries_transient_storage_failures(self) -> None:
        self.quota.settle_image_quota.side_effect = [
            RuntimeError("storage busy"),
            RuntimeError("storage busy"),
            True,
        ]
        ai_module.openai_v1_image_generations.handle.return_value = iter(
            [{"object": "image.generation.result", "data": [{"url": "stream-image"}]}]
        )

        with mock.patch.object(ai_module.time, "sleep", return_value=None):
            response = self.client.post(
                "/v1/images/generations",
                headers=AUTH_HEADERS,
                json={"prompt": "cat", "model": "gpt-image-2", "n": 1, "stream": True},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.quota.settle_image_quota.call_count, 3)

    def test_stream_settlement_failure_does_not_break_completed_stream(self) -> None:
        self.quota.settle_image_quota.side_effect = RuntimeError("storage unavailable")
        ai_module.openai_v1_image_generations.handle.return_value = iter(
            [{"object": "image.generation.result", "data": [{"url": "stream-image"}]}]
        )

        with (
            mock.patch.object(ai_module.time, "sleep", return_value=None),
            mock.patch.object(ai_module.logger, "error") as log_error,
        ):
            response = self.client.post(
                "/v1/images/generations",
                headers=AUTH_HEADERS,
                json={"prompt": "cat", "model": "gpt-image-2", "n": 1, "stream": True},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("stream-image", response.text)
        self.assertEqual(self.quota.settle_image_quota.call_count, 3)
        log_error.assert_called_once()

    def test_chat_markdown_images_are_counted_for_settlement(self) -> None:
        ai_module.openai_v1_chat_complete.handle.return_value = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "![image_1](data:image/png;base64,Zmlyc3Q=)\n\n"
                    "![image_2](data:image/png;base64,Zmlyc3Q=)",
                }
            }]
        }

        response = self.client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={"model": "gpt-image-2-high", "n": 2, "messages": [{"role": "user", "content": "cat"}]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.quota.settle_image_quota.call_args.args, ("user-1", 2))

    def test_streamed_chat_markdown_images_are_counted_for_settlement(self) -> None:
        ai_module.openai_v1_chat_complete.handle.return_value = iter([
            {"choices": [{"delta": {"content": "![image_1](data:image/png;base64,Zmlyc3Q=)"}}]},
            {"choices": [{"delta": {"content": "![image_2](data:image/png;base64,Zmlyc3Q=)"}}]},
        ])

        response = self.client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2-high",
                "n": 2,
                "stream": True,
                "messages": [{"role": "user", "content": "cat"}],
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.quota.settle_image_quota.call_args.args, ("user-1", 2))

    def test_responses_image_generation_items_are_counted_for_settlement(self) -> None:
        ai_module.openai_v1_response.handle.return_value = {
            "output": [
                {"id": "ig_1", "type": "image_generation_call", "result": "Zmlyc3Q="},
                {"id": "ig_2", "type": "image_generation_call", "result": "c2Vjb25k"},
            ]
        }

        response = self.client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={"model": "gpt-image-2", "n": 2, "input": "cat", "tools": [{"type": "image_generation"}]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.quota.settle_image_quota.call_args.args, ("user-1", 2))


if __name__ == "__main__":
    unittest.main()
