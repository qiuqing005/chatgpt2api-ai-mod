from __future__ import annotations

import copy
import time
import unittest

from services.auth_service import (
    API_IMAGE_RESERVATION_PREFIX,
    API_IMAGE_RESERVATION_TTL_SECS,
    AuthService,
    ImageQuotaExceeded,
    ImageQuotaStorageError,
    MAX_IMAGE_QUOTA,
)
from services.storage.base import StorageBackend


class MemoryStorage(StorageBackend):
    def __init__(self) -> None:
        self.accounts: list[dict] = []
        self.auth_keys: list[dict] = []
        self.fail_load = False

    def load_accounts(self) -> list[dict]:
        return copy.deepcopy(self.accounts)

    def save_accounts(self, accounts: list[dict]) -> None:
        self.accounts = copy.deepcopy(accounts)

    def load_auth_keys(self) -> list[dict]:
        if self.fail_load:
            raise RuntimeError("storage unavailable")
        return copy.deepcopy(self.auth_keys)

    def save_auth_keys(self, auth_keys: list[dict]) -> None:
        self.auth_keys = copy.deepcopy(auth_keys)

    def health_check(self) -> dict:
        return {"ok": True}

    def get_backend_info(self) -> dict:
        return {"backend": "memory"}


class AuthImageQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()
        self.service = AuthService(self.storage)

    def test_limited_user_reserves_and_refunds_quota(self) -> None:
        item, raw_key = self.service.create_key(role="user", name="limited", image_quota=2)
        identity = self.service.authenticate(raw_key)
        self.assertIsNotNone(identity)

        self.assertTrue(self.service.reserve_image_quota(identity or {}, 2))
        with self.assertRaises(ImageQuotaExceeded):
            self.service.reserve_image_quota(identity or {}, 1)

        self.service.refund_image_quota(str(item["id"]), 1)
        profile = self.service.authenticate(raw_key)
        self.assertEqual(profile["image_used"], 1)
        self.assertEqual(profile["image_remaining"], 1)

    def test_zero_quota_is_explicitly_unlimited(self) -> None:
        _, raw_key = self.service.create_key(role="user", name="unlimited", image_quota=0)
        identity = self.service.authenticate(raw_key)

        self.assertFalse(self.service.reserve_image_quota(identity or {}, 4))
        profile = self.service.authenticate(raw_key)
        self.assertEqual(profile["image_used"], 0)
        self.assertIsNone(profile["image_remaining"])

    def test_reservation_refund_is_idempotent(self) -> None:
        item, raw_key = self.service.create_key(role="user", name="reserved", image_quota=3)
        identity = self.service.authenticate(raw_key)

        self.assertTrue(
            self.service.reserve_image_quota(identity or {}, 1, reservation_id="task-1")
        )
        self.assertTrue(
            self.service.reserve_image_quota(identity or {}, 1, reservation_id="task-1")
        )
        self.assertTrue(
            self.service.refund_image_quota(str(item["id"]), 1, reservation_id="task-1")
        )
        self.assertFalse(
            self.service.refund_image_quota(str(item["id"]), 1, reservation_id="task-1")
        )

        profile = self.service.authenticate(raw_key)
        self.assertEqual(profile["image_used"], 0)

    def test_committed_reservation_cannot_be_refunded(self) -> None:
        item, raw_key = self.service.create_key(role="user", name="committed", image_quota=3)
        identity = self.service.authenticate(raw_key)

        self.assertTrue(
            self.service.reserve_image_quota(identity or {}, 1, reservation_id="task-2")
        )
        self.assertTrue(
            self.service.commit_image_quota(str(item["id"]), reservation_id="task-2")
        )
        self.assertFalse(
            self.service.refund_image_quota(str(item["id"]), 1, reservation_id="task-2")
        )

        profile = self.service.authenticate(raw_key)
        self.assertEqual(profile["image_used"], 1)

    def test_reservation_settlement_charges_only_successful_images(self) -> None:
        item, raw_key = self.service.create_key(role="user", name="settled", image_quota=4)
        identity = self.service.authenticate(raw_key)

        self.service.reserve_image_quota(identity or {}, 3, reservation_id="api:test")
        self.assertTrue(
            self.service.settle_image_quota(str(item["id"]), 1, reservation_id="api:test")
        )
        self.assertFalse(
            self.service.settle_image_quota(str(item["id"]), 1, reservation_id="api:test")
        )

        profile = self.service.authenticate(raw_key)
        self.assertEqual(profile["image_used"], 1)
        self.assertEqual(profile["image_remaining"], 3)

    def test_startup_recovers_api_reservations_but_preserves_task_reservations(self) -> None:
        item, raw_key = self.service.create_key(role="user", name="recover", image_quota=5)
        identity = self.service.authenticate(raw_key)
        self.service.reserve_image_quota(
            identity or {},
            2,
            reservation_id=f"{API_IMAGE_RESERVATION_PREFIX}request-1",
        )
        self.service.reserve_image_quota(identity or {}, 1, reservation_id="task-1")

        recovered = AuthService(self.storage)
        profile = recovered.authenticate(raw_key)

        self.assertEqual(profile["image_used"], 1)
        self.assertTrue(
            recovered.refund_image_quota(str(item["id"]), 1, reservation_id="task-1")
        )

    def test_new_reservation_recovers_expired_api_reservations(self) -> None:
        _, raw_key = self.service.create_key(role="user", name="expired", image_quota=4)
        identity = self.service.authenticate(raw_key)
        expired_id = (
            f"{API_IMAGE_RESERVATION_PREFIX}"
            f"{int(time.time()) - API_IMAGE_RESERVATION_TTL_SECS - 1}:request-1"
        )
        self.service.reserve_image_quota(identity or {}, 2, reservation_id=expired_id)

        self.service.reserve_image_quota(identity or {}, 1, reservation_id="task-current")

        profile = self.service.authenticate(raw_key)
        self.assertEqual(profile["image_used"], 1)
        self.assertEqual(profile["image_remaining"], 3)

    def test_quota_values_are_bounded(self) -> None:
        item, _ = self.service.create_key(role="user", image_quota=MAX_IMAGE_QUOTA * 100)
        self.assertEqual(item["image_quota"], MAX_IMAGE_QUOTA)

    def test_storage_failure_does_not_become_unlimited_quota(self) -> None:
        _, raw_key = self.service.create_key(role="user", image_quota=2)
        identity = self.service.authenticate(raw_key)
        self.storage.fail_load = True

        with self.assertRaises(ImageQuotaStorageError):
            self.service.reserve_image_quota(identity or {}, 1)

    def test_missing_user_does_not_become_unlimited_quota(self) -> None:
        _, raw_key = self.service.create_key(role="user", image_quota=2)
        identity = self.service.authenticate(raw_key)
        self.storage.auth_keys = []

        with self.assertRaises(ImageQuotaStorageError):
            self.service.reserve_image_quota(identity or {}, 1)

    def test_usage_reset_preserves_inflight_reservations(self) -> None:
        item, raw_key = self.service.create_key(role="user", image_quota=3)
        identity = self.service.authenticate(raw_key)
        self.service.reserve_image_quota(identity or {}, 1, reservation_id="task-1")
        self.service.reserve_image_quota(identity or {}, 1, reservation_id="task-2")

        updated = self.service.update_key(str(item["id"]), {"image_used": 0}, role="user")

        self.assertEqual(updated["image_used"], 2)
        with self.assertRaisesRegex(ValueError, "已使用额度"):
            self.service.update_key(str(item["id"]), {"image_quota": 1}, role="user")

    def test_quota_cannot_be_lowered_below_completed_usage(self) -> None:
        item, raw_key = self.service.create_key(role="user", image_quota=5)
        identity = self.service.authenticate(raw_key)
        self.service.reserve_image_quota(identity or {}, 3)

        with self.assertRaisesRegex(ValueError, "已使用额度 3"):
            self.service.update_key(str(item["id"]), {"image_quota": 2}, role="user")


if __name__ == "__main__":
    unittest.main()
