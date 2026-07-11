from __future__ import annotations

import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from services.config import config
from services.image_task_service import (
    MAX_CLIENT_TASK_ID_LENGTH,
    DaemonWorkerPool,
    ImageTaskQueueUnavailable,
    ImageTaskStorageError,
    ImageTaskService,
    _quota_reservation_id,
)


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}


class FakeQuotaService:
    def __init__(self, reserve: bool = True) -> None:
        self.reserve = reserve
        self.reserve_calls: list[tuple[str, int, str]] = []
        self.refund_calls: list[tuple[str, int, str]] = []
        self.commit_calls: list[tuple[str, str]] = []

    def reserve_image_quota(
        self,
        identity: dict[str, object],
        amount: int,
        *,
        reservation_id: str = "",
    ) -> bool:
        self.reserve_calls.append((str(identity.get("id") or ""), amount, reservation_id))
        return self.reserve

    def refund_image_quota(self, owner_id: str, amount: int, *, reservation_id: str = "") -> bool:
        self.refund_calls.append((owner_id, amount, reservation_id))
        return True

    def commit_image_quota(self, owner_id: str, *, reservation_id: str) -> bool:
        self.commit_calls.append((owner_id, reservation_id))
        return True


def wait_for_task(service: ImageTaskService, identity: dict[str, object], task_id: str, status: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = service.list_tasks(identity, [task_id])
        last = (result.get("items") or [None])[0]
        if last and last.get("status") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {status}, last={last}")


class ImageTaskServiceTests(unittest.TestCase):
    def make_service(self, path: Path, handler=None, quota_service=None, workers: int = 2) -> ImageTaskService:
        return ImageTaskService(
            path,
            generation_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/image.png"}]}),
            edit_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/edit.png"}]}),
            retention_days_getter=lambda: 30,
            quota_service=quota_service or FakeQuotaService(reserve=False),
            task_workers_getter=lambda: workers,
        )

    def test_duplicate_submit_uses_existing_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls = 0

            def handler(_payload):
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            first = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            second = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            self.assertEqual(first["id"], "task-1")
            self.assertEqual(second["id"], "task-1")
            task = wait_for_task(service, OWNER, "task-1", "success")
            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")
            self.assertEqual(calls, 1)

    def test_different_owner_cannot_query_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            service.submit_generation(
                OWNER,
                client_task_id="private-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            wait_for_task(service, OWNER, "private-task", "success")
            result = service.list_tasks(OTHER_OWNER, ["private-task"])

            self.assertEqual(result["items"], [])
            self.assertEqual(result["missing_ids"], ["private-task"])

    def test_success_task_persists_to_new_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            service.submit_generation(
                OWNER,
                client_task_id="persisted-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OWNER, "persisted-task", "success")

            reloaded = self.make_service(path)
            result = reloaded.list_tasks(OWNER, ["persisted-task"])

            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(result["items"][0]["status"], "success")
            self.assertEqual(result["items"][0]["data"][0]["url"], "http://example.test/image.png")

    def test_startup_marks_unfinished_tasks_as_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "queued-task",
                                "owner_id": "owner-1",
                                "status": "queued",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                            {
                                "id": "running-task",
                                "owner_id": "owner-1",
                                "status": "running",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            service = self.make_service(path)
            result = service.list_tasks(OWNER, ["queued-task", "running-task"])

            self.assertEqual([item["status"] for item in result["items"]], ["error", "error"])
            self.assertTrue(all("已中断" in item.get("error", "") for item in result["items"]))

    def test_failed_task_refunds_reserved_quota(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            quota = FakeQuotaService()

            def handler(_payload):
                raise RuntimeError("upstream failed")

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler, quota)
            service.submit_generation(
                OTHER_OWNER,
                client_task_id="refund-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OTHER_OWNER, "refund-task", "error")

            reservation_id = _quota_reservation_id("owner-2:refund-task")
            self.assertEqual(quota.reserve_calls, [("owner-2", 1, reservation_id)])
            self.assertEqual(quota.refund_calls, [("owner-2", 1, reservation_id)])

    def test_duplicate_task_only_reserves_once(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            quota = FakeQuotaService()
            service = self.make_service(Path(tmp_dir) / "image_tasks.json", quota_service=quota)

            for _ in range(2):
                service.submit_generation(
                    OTHER_OWNER,
                    client_task_id="same-task",
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                    base_url="http://local.test",
                )
            wait_for_task(service, OTHER_OWNER, "same-task", "success")

            reservation_id = _quota_reservation_id("owner-2:same-task")
            self.assertEqual(quota.reserve_calls, [("owner-2", 1, reservation_id)])
            self.assertEqual(quota.refund_calls, [])
            self.assertEqual(quota.commit_calls, [("owner-2", reservation_id)])

    def test_worker_pool_bounds_concurrent_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            release = threading.Event()
            entered = threading.Event()
            lock = threading.Lock()
            active = 0
            max_active = 0

            def handler(_payload):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    entered.set()
                release.wait(2)
                with lock:
                    active -= 1
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler, workers=1)
            for index in range(3):
                service.submit_generation(
                    OWNER,
                    client_task_id=f"queued-{index}",
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                    base_url="http://local.test",
                )

            self.assertTrue(entered.wait(1))
            time.sleep(0.05)
            self.assertEqual(max_active, 1)
            release.set()
            for index in range(3):
                wait_for_task(service, OWNER, f"queued-{index}", "success")

    def test_task_snapshots_backend_model_when_submitted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            captured: list[dict] = []

            def handler(payload):
                captured.append(dict(payload))
                return {"data": [{"url": "http://example.test/image.png"}]}

            first = {"base_model": "gpt-5.6-sol", "thinking_model": "gpt-5.6-sol", "fallback_enabled": True}
            second = {"base_model": "gpt-5-5", "thinking_model": "gpt-5-5-thinking", "fallback_enabled": False}
            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            with mock.patch.dict(config.data, {"image_model_routing": first}):
                service.submit_generation(
                    OWNER,
                    client_task_id="snapshot-task",
                    prompt="cat",
                    model="gpt-image-2-high",
                    size=None,
                    base_url="http://local.test",
                )
                config.data["image_model_routing"] = second
                wait_for_task(service, OWNER, "snapshot-task", "success")

            self.assertEqual(captured[0]["_image_backend_model"], "gpt-5.6-sol")
            self.assertEqual(captured[0]["_image_thinking_effort"], "extended")
            self.assertTrue(captured[0]["_image_fallback_enabled"])

    def test_startup_refunds_tracked_reservation_even_before_flag_was_saved(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "crash-window",
                                "owner_id": "owner-2",
                                "status": "queued",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "quota_reserved": False,
                                "quota_reservation_tracked": True,
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            quota = FakeQuotaService()

            service = self.make_service(path, quota_service=quota)
            task = service.list_tasks(OTHER_OWNER, ["crash-window"])["items"][0]

            self.assertEqual(task["status"], "error")
            self.assertEqual(
                quota.refund_calls,
                [("owner-2", 1, _quota_reservation_id("owner-2:crash-window"))],
            )

    def test_timeout_detection_keeps_reservation_for_followup_poll(self):
        self.assertTrue(ImageTaskService._is_resumable_timeout("poll timed out", "conversation-1"))
        self.assertTrue(ImageTaskService._is_resumable_timeout("继续等待后仍未找到图片结果", "conversation-1"))
        self.assertFalse(ImageTaskService._is_resumable_timeout("poll timed out", ""))

    def test_client_task_id_length_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")

            with self.assertRaisesRegex(ValueError, "must not exceed"):
                service.submit_generation(
                    OWNER,
                    client_task_id="x" * (MAX_CLIENT_TASK_ID_LENGTH + 1),
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                )

    def test_worker_pool_rejects_when_queue_is_full(self):
        release = threading.Event()
        entered = threading.Event()
        pool = DaemonWorkerPool(1, name_prefix="bounded-test")

        def blocker():
            entered.set()
            release.wait(2)

        pool.submit(blocker)
        self.assertTrue(entered.wait(1))
        for _ in range(pool._queue.maxsize):
            pool.submit(lambda: None)
        with self.assertRaises(queue.Full):
            pool.submit(lambda: None)
        release.set()
        pool.close(wait=True)

    def test_queue_failure_refunds_reserved_quota(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            quota = FakeQuotaService()
            service = self.make_service(Path(tmp_dir) / "image_tasks.json", quota_service=quota)
            with mock.patch.object(service._executor, "submit", side_effect=queue.Full):
                with self.assertRaises(ImageTaskQueueUnavailable):
                    service.submit_generation(
                        OTHER_OWNER,
                        client_task_id="queue-full",
                        prompt="cat",
                        model="gpt-image-2",
                        size=None,
                    )

            reservation_id = _quota_reservation_id("owner-2:queue-full")
            self.assertEqual(quota.refund_calls, [("owner-2", 1, reservation_id)])
            self.assertEqual(service.list_tasks(OTHER_OWNER, ["queue-full"])["missing_ids"], ["queue-full"])

            retried = service.submit_generation(
                OTHER_OWNER,
                client_task_id="queue-full",
                prompt="cat",
                model="gpt-image-2",
                size=None,
            )
            self.assertIn(retried["status"], {"queued", "running", "success"})
            wait_for_task(service, OTHER_OWNER, "queue-full", "success")

    def test_malformed_task_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(ImageTaskStorageError, "账本无法读取"):
                self.make_service(path)

    def test_resumable_timeout_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            key = "owner-2:timeout-task"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "timeout-task",
                                "owner_id": "owner-2",
                                "status": "error",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "error": "poll timed out",
                                "conversation_id": "conversation-1",
                                "quota_reserved": True,
                                "quota_reservation_tracked": True,
                                "quota_reservation_id": _quota_reservation_id(key),
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            quota = FakeQuotaService()
            service = self.make_service(path, quota_service=quota)
            with mock.patch.object(service._executor, "submit") as submit:
                task = service.resume_poll(OTHER_OWNER, "timeout-task")

            self.assertEqual(task["status"], "running")
            self.assertEqual(quota.refund_calls, [])
            submit.assert_called_once()

    def test_resume_queue_failure_keeps_timeout_retryable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            key = "owner-2:timeout-task"
            path.write_text(
                json.dumps({
                    "tasks": [{
                        "id": "timeout-task",
                        "owner_id": "owner-2",
                        "status": "error",
                        "mode": "generate",
                        "model": "gpt-image-2",
                        "error": "poll timed out",
                        "conversation_id": "conversation-1",
                        "quota_reserved": True,
                        "quota_reservation_tracked": True,
                        "quota_reservation_id": _quota_reservation_id(key),
                        "created_at": "2099-01-01 00:00:00",
                        "updated_at": "2099-01-01 00:00:00",
                    }]
                }),
                encoding="utf-8",
            )
            quota = FakeQuotaService()
            service = self.make_service(path, quota_service=quota)

            with mock.patch.object(service._executor, "submit", side_effect=queue.Full):
                with self.assertRaises(ImageTaskQueueUnavailable):
                    service.resume_poll(OTHER_OWNER, "timeout-task")

            task = service.list_tasks(OTHER_OWNER, ["timeout-task"])["items"][0]
            self.assertEqual(task["status"], "error")
            self.assertEqual(task["error"], "poll timed out")
            self.assertEqual(quota.refund_calls, [])

            with mock.patch.object(service._executor, "submit") as submit:
                service.resume_poll(OTHER_OWNER, "timeout-task")
            submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
