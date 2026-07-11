from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.process_lock import SingleProcessLock


class SingleProcessLockTests(unittest.TestCase):
    def test_same_data_directory_cannot_be_locked_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / ".instance.lock"
            first = SingleProcessLock(path)
            second = SingleProcessLock(path)
            first.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "already using data directory"):
                    second.acquire()
            finally:
                first.release()

            second.acquire()
            second.release()

    def test_forked_process_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock = SingleProcessLock(Path(tmp_dir) / ".instance.lock")
            lock.acquire()
            try:
                with (
                    mock.patch("services.process_lock.os.getpid", return_value=(lock._owner_pid or 0) + 1),
                    self.assertRaisesRegex(RuntimeError, "forked application workers"),
                ):
                    lock.assert_current_process()
            finally:
                lock.release()


if __name__ == "__main__":
    unittest.main()
