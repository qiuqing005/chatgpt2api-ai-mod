from __future__ import annotations

import atexit
import os
from pathlib import Path
from typing import BinaryIO


class SingleProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None
        self._owner_pid: int | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                f"another chatgpt2api process is already using data directory: {self.path.parent}"
            ) from exc
        self._handle = handle
        self._owner_pid = os.getpid()
        atexit.register(self.release)

    def assert_current_process(self) -> None:
        if self._handle is None:
            self.acquire()
            return
        if self._owner_pid != os.getpid():
            raise RuntimeError(
                "preloaded or forked application workers are unsupported; run exactly one process"
            )

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        owner_pid = self._owner_pid
        self._owner_pid = None
        if owner_pid != os.getpid():
            handle.close()
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
