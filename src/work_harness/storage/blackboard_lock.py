"""One operating-system lock per database, shared by handles in the same process."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from work_harness.blackboard.protocol import BlackboardProcessLockError


@dataclass
class _HeldLock:
    stream: BinaryIO
    pid: int
    session_id: str
    references: int = 1


_mutex = threading.Lock()
_held: dict[Path, _HeldLock] = {}


def _os_lock(stream: BinaryIO, *, unlock: bool = False) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


class BlackboardProcessLock:
    def __init__(self, database_path: Path, *, reentrant: bool = True) -> None:
        self.path = Path(str(database_path.resolve()) + ".lock")
        self._entry: _HeldLock | None = None
        self._reentrant = reentrant

    @property
    def session_id(self) -> str:
        if self._entry is None or self._entry.pid != os.getpid():
            raise BlackboardProcessLockError("this process does not hold the blackboard lock")
        return self._entry.session_id

    def acquire(self) -> None:
        with _mutex:
            if self._entry is not None and self._entry.pid == os.getpid():
                return
            entry = _held.get(self.path)
            if entry is not None and entry.pid != os.getpid():
                # A fork must acquire its own lock, never unlock its parent's descriptor.
                entry.stream.close()
                del _held[self.path]
                entry = None
            if entry is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                stream = self.path.open("a+b")
                try:
                    _os_lock(stream)
                except OSError as exc:
                    stream.close()
                    raise BlackboardProcessLockError(
                        f"another process owns the blackboard database: {self.path}"
                    ) from exc
                entry = _HeldLock(stream, os.getpid(), uuid4().hex)
                _held[self.path] = entry
            else:
                if not self._reentrant:
                    raise BlackboardProcessLockError(
                        f"database is in use; stop the runtime before backup: {self.path}"
                    )
                entry.references += 1
            self._entry = entry

    def release(self) -> None:
        with _mutex:
            entry, self._entry = self._entry, None
            if entry is None:
                return
            if entry.pid != os.getpid():
                entry.stream.close()
                return
            entry.references -= 1
            if entry.references == 0:
                try:
                    _os_lock(entry.stream, unlock=True)
                finally:
                    entry.stream.close()
                    del _held[self.path]
                # Keep the lock file: unlinking it could let another inode be locked concurrently.
