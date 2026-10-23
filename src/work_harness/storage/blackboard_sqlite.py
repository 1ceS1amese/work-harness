"""SQLite blackboard history, projections and atomic publication receipts."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from pydantic import TypeAdapter

from work_harness.blackboard.control import (
    check_writer,
    claim_writer,
    close_control,
    release_writer,
    seal_snapshot,
)
from work_harness.blackboard.models import BoardObject, ContentRef, Fact, ObjectRef
from work_harness.blackboard.protocol import (
    BlackboardError,
    BoardControl,
    BoardEvent,
    BoardFinalization,
    BoardNotFoundError,
    BoardSnapshot,
    CommitReceipt,
    EventWindow,
    HistoryUnavailableError,
    IdempotencyConflictError,
    InputPendingError,
    ObjectRevision,
    StoredContent,
    TerminalStatus,
    WriterToken,
)
from work_harness.blackboard.store import CommitRequest, validate_commit, validate_publication
from work_harness.storage.async_utils import write_in_thread
from work_harness.storage.blackboard_lock import BlackboardProcessLock

_Result = TypeVar("_Result")
_OBJECT = TypeAdapter(BoardObject)
_SCHEMA_VERSION = 9
_CONTROL_TABLE = """
    CREATE TABLE IF NOT EXISTS board_control (
        board_id TEXT PRIMARY KEY REFERENCES boards(board_id),
        payload TEXT,
        generation INTEGER NOT NULL DEFAULT 0,
        owner_id TEXT,
        session_id TEXT,
        cancelled INTEGER NOT NULL DEFAULT 0,
        finalization TEXT,
        wakeup_revision INTEGER NOT NULL DEFAULT 0,
        status_pending INTEGER NOT NULL DEFAULT 0
    )
"""
_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS boards (
        board_id TEXT PRIMARY KEY,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        root_intent_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS board_object_revisions (
        board_id TEXT NOT NULL REFERENCES boards(board_id),
        object_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        board_revision INTEGER NOT NULL CHECK (board_revision >= 1),
        payload TEXT NOT NULL,
        PRIMARY KEY (board_id, object_id, revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS board_objects_current (
        board_id TEXT NOT NULL,
        object_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('fact', 'intent', 'hint')),
        topic TEXT NOT NULL,
        status TEXT NOT NULL,
        PRIMARY KEY (board_id, object_id),
        FOREIGN KEY (board_id, object_id, revision)
            REFERENCES board_object_revisions(board_id, object_id, revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS board_events (
        board_id TEXT NOT NULL REFERENCES boards(board_id),
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        payload TEXT NOT NULL,
        PRIMARY KEY (board_id, sequence)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS board_commits (
        board_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        board_revision INTEGER NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (board_id, idempotency_key),
        UNIQUE (board_id, board_revision),
        FOREIGN KEY (board_id, board_revision) REFERENCES board_events(board_id, sequence)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS board_contents (
        board_id TEXT NOT NULL REFERENCES boards(board_id),
        content_digest TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (board_id, content_digest)
    )
    """,
    """CREATE INDEX IF NOT EXISTS ix_board_objects_query
       ON board_objects_current(board_id, kind, topic, status)""",
    """CREATE INDEX IF NOT EXISTS ix_board_history_snapshot
       ON board_object_revisions(board_id, board_revision)""",
    _CONTROL_TABLE,
    """
    CREATE TABLE IF NOT EXISTS board_activations (
        activation_id TEXT PRIMARY KEY,
        board_id TEXT NOT NULL REFERENCES boards(board_id),
        UNIQUE (board_id, activation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS board_activation_attempts (
        activation_id TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt >= 1),
        board_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
        payload TEXT NOT NULL,
        PRIMARY KEY (activation_id, attempt),
        UNIQUE (board_id, ordinal),
        FOREIGN KEY (board_id, activation_id) REFERENCES board_activations(board_id, activation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS board_usage (
        activation_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        call_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (activation_id, attempt, call_id),
        FOREIGN KEY (activation_id, attempt)
            REFERENCES board_activation_attempts(activation_id, attempt)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS board_verification_receipts (
        board_id TEXT NOT NULL REFERENCES boards(board_id),
        receipt_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (board_id, receipt_id)
    )
    """,
)


class SQLiteBlackboardStore:
    """One transaction per operation, using short-lived connections off the event loop."""

    backend = "sqlite"

    def __init__(self, path: Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("use MemoryBlackboardStore for an in-memory blackboard")
        self.path = Path(path).resolve()
        self._process_lock = BlackboardProcessLock(self.path)
        self._lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    async def _thread(self, operation: Callable[[], _Result]) -> _Result:
        return await write_in_thread(operation)

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            try:
                await self._thread(self._initialize)
            except BaseException:
                self._process_lock.release()
                raise
            self._initialized = True

    def _initialize(self) -> None:
        self._process_lock.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS blackboard_schema "
                    "(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
                )
                row = connection.execute(
                    "SELECT version FROM blackboard_schema WHERE id = 1"
                ).fetchone()
                if row is not None and row["version"] not in (
                    1,
                    2,
                    3,
                    4,
                    5,
                    6,
                    7,
                    8,
                    _SCHEMA_VERSION,
                ):
                    raise BlackboardError(
                        f"unsupported blackboard schema version: {row['version']}"
                    )
                columns = {
                    item["name"] for item in connection.execute("PRAGMA table_info(board_control)")
                }
                if columns and "generation" not in columns:
                    connection.execute("ALTER TABLE board_control RENAME TO board_control_v2")
                    connection.execute(_CONTROL_TABLE)
                    connection.execute(
                        "INSERT INTO board_control (board_id, payload) "
                        "SELECT board_id, payload FROM board_control_v2"
                    )
                    connection.execute("DROP TABLE board_control_v2")
                for statement in _TABLES:
                    connection.execute(statement)
                columns = {
                    item["name"] for item in connection.execute("PRAGMA table_info(board_control)")
                }
                if "wakeup_revision" not in columns:
                    connection.execute(
                        "ALTER TABLE board_control ADD COLUMN wakeup_revision "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                if "status_pending" not in columns:
                    connection.execute(
                        "ALTER TABLE board_control ADD COLUMN status_pending "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                    connection.execute(
                        "UPDATE board_control SET status_pending = 1 WHERE finalization IS NOT NULL"
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO board_control (board_id) SELECT board_id FROM boards"
                )
                connection.execute(
                    "INSERT INTO blackboard_schema (id, version) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
                    (_SCHEMA_VERSION,),
                )

    async def close(self) -> None:
        async with self._lock:
            self._initialized = False
            self._process_lock.release()

    async def transaction(
        self, operation: Callable[[sqlite3.Connection], _Result], *, write: bool = False
    ) -> _Result:
        """Host storage adapters share this connection and transaction boundary."""

        def transaction() -> _Result:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                return operation(connection)

        async with self._lock:
            if not self._initialized:
                raise BlackboardError("blackboard store must be initialized before use")
            _ = self._process_lock.session_id
            return await self._thread(transaction)

    @staticmethod
    def _control(connection: sqlite3.Connection, board_id: str) -> BoardControl:
        row = connection.execute(
            "SELECT * FROM board_control WHERE board_id = ?", (board_id,)
        ).fetchone()
        if row is None:
            raise BoardNotFoundError(f"unknown board: {board_id}")
        return BoardControl(
            board_id=board_id,
            generation=row["generation"],
            owner_id=row["owner_id"],
            session_id=row["session_id"],
            cancelled=bool(row["cancelled"]),
            finalization=BoardFinalization.model_validate_json(row["finalization"])
            if row["finalization"]
            else None,
            wakeup_revision=row["wakeup_revision"],
            status_pending=bool(row["status_pending"]),
        )

    @staticmethod
    def _save_control(connection: sqlite3.Connection, control: BoardControl) -> None:
        connection.execute(
            """UPDATE board_control SET generation = ?, owner_id = ?, session_id = ?,
               cancelled = ?, finalization = ?, wakeup_revision = ?, status_pending = ?
               WHERE board_id = ?""",
            (
                control.generation,
                control.owner_id,
                control.session_id,
                control.cancelled,
                control.finalization.model_dump_json() if control.finalization else None,
                control.wakeup_revision,
                int(control.status_pending),
                control.board_id,
            ),
        )

    async def control(self, board_id: str) -> BoardControl:
        return await self.transaction(lambda connection: self._control(connection, board_id))

    async def pending_reconciliations(self, *, limit: int = 200) -> tuple[str, ...]:
        return await self.transaction(
            lambda connection: tuple(
                row["board_id"]
                for row in connection.execute(
                    "SELECT board_id FROM board_control WHERE wakeup_revision > 0 "
                    "OR status_pending = 1 ORDER BY board_id LIMIT ?",
                    (limit,),
                )
            )
        )

    async def release_writer(self, writer: WriterToken) -> None:
        await self.transaction(
            lambda connection: self._save_control(
                connection,
                release_writer(
                    self._control(connection, writer.board_id),
                    writer,
                    self._process_lock.session_id,
                ),
            ),
            write=True,
        )

    async def acknowledge_finalization(self, board_id: str, revision: int) -> None:
        def acknowledge(connection: sqlite3.Connection) -> None:
            control = self._control(connection, board_id)
            if (
                control.finalization is None
                or control.finalization.finalized_at_revision != revision
            ):
                raise BlackboardError("finalization acknowledgment does not match the board")
            self._save_control(connection, control.model_copy(update={"status_pending": False}))

        await self.transaction(acknowledge, write=True)

    async def acknowledge_wakeup(self, board_id: str, through: int, *, writer: WriterToken) -> None:
        def acknowledge(connection: sqlite3.Connection) -> None:
            control = self._control(connection, board_id)
            check_writer(control, writer, self._process_lock.session_id)
            if control.wakeup_revision <= through:
                self._save_control(connection, control.model_copy(update={"wakeup_revision": 0}))

        await self.transaction(acknowledge, write=True)

    async def claim_writer(
        self, board_id: str, owner_id: str, *, takeover: bool = False
    ) -> WriterToken:
        def claim(connection: sqlite3.Connection) -> WriterToken:
            updated, token = claim_writer(
                self._control(connection, board_id),
                owner_id,
                self._process_lock.session_id,
                takeover=takeover,
            )
            self._save_control(connection, updated)
            return token

        return await self.transaction(claim, write=True)

    async def check_writer(self, writer: WriterToken) -> None:
        await self.transaction(
            lambda connection: self.check_writer_in_transaction(connection, writer)
        )

    def check_writer_in_transaction(
        self, connection: sqlite3.Connection, writer: WriterToken
    ) -> None:
        check_writer(
            self._control(connection, writer.board_id), writer, self._process_lock.session_id
        )

    async def finalize(
        self,
        board_id: str,
        *,
        status: TerminalStatus,
        reason: str,
        writer: WriterToken | None,
        clock: Callable[[], datetime],
        attempted_directions: tuple[str, ...] = (),
        observed_revision: int | None = None,
    ) -> BoardFinalization:
        def finalize(connection: sqlite3.Connection) -> BoardFinalization:
            control = self._control(connection, board_id)
            if control.finalization is not None:
                return control.finalization
            if status != "cancelled":
                if writer is None:
                    raise BlackboardError("finalization requires a writer token")
                check_writer(control, writer, self._process_lock.session_id)
                if control.wakeup_revision > (observed_revision or 0):
                    raise InputPendingError("new human input must be assessed before finalization")
            updated, event, final = seal_snapshot(
                self._snapshot(connection, board_id), status, reason, clock(), attempted_directions
            )
            self._publish_objects(connection, updated, event)
            connection.execute(
                "UPDATE boards SET revision = ? WHERE board_id = ?", (updated.revision, board_id)
            )
            self._save_control(connection, close_control(control, final))
            return final

        return await self.transaction(finalize, write=True)

    @staticmethod
    def _board(connection: sqlite3.Connection, board_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM boards WHERE board_id = ?", (board_id,)).fetchone()
        if row is None:
            raise BoardNotFoundError(f"unknown board: {board_id}")
        return row

    def _snapshot(
        self, connection: sqlite3.Connection, board_id: str, revision: int | None = None
    ) -> BoardSnapshot:
        board = self._board(connection, board_id)
        version = board["revision"] if revision is None else revision
        if not 1 <= version <= board["revision"]:
            raise BlackboardError("unknown board revision")
        if version == board["revision"]:
            rows = connection.execute(
                """SELECT r.payload FROM board_objects_current AS c
                   JOIN board_object_revisions AS r USING (board_id, object_id, revision)
                   WHERE c.board_id = ? ORDER BY c.object_id""",
                (board_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT r.payload FROM board_object_revisions AS r
                   JOIN (
                       SELECT object_id, MAX(revision) AS revision
                       FROM board_object_revisions
                       WHERE board_id = ? AND board_revision <= ? GROUP BY object_id
                   ) AS latest USING (object_id, revision)
                   WHERE r.board_id = ? ORDER BY r.object_id""",
                (board_id, version, board_id),
            ).fetchall()
        final = self._control(connection, board_id).finalization
        return BoardSnapshot(
            board_id=board_id,
            revision=version,
            root_intent_id=board["root_intent_id"],
            objects=tuple(_OBJECT.validate_json(row["payload"]) for row in rows),
            finalization=final
            if final is not None and final.finalized_at_revision <= version
            else None,
        )

    async def snapshot(self, board_id: str, *, revision: int | None = None) -> BoardSnapshot:
        return await self.transaction(
            lambda connection: self._snapshot(connection, board_id, revision)
        )

    @staticmethod
    def _publish_objects(
        connection: sqlite3.Connection, snapshot: BoardSnapshot, event: BoardEvent
    ) -> None:
        changed = {ref.object_id for ref in event.changed_refs}
        for item in snapshot.objects:
            if item.id not in changed:
                continue
            connection.execute(
                """INSERT INTO board_object_revisions
                   (board_id, object_id, revision, board_revision, payload)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    snapshot.board_id,
                    item.id,
                    item.revision,
                    snapshot.revision,
                    item.model_dump_json(),
                ),
            )
            connection.execute(
                """INSERT INTO board_objects_current
                   (board_id, object_id, revision, kind, topic, status) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(board_id, object_id) DO UPDATE SET
                   revision = excluded.revision, kind = excluded.kind,
                   topic = excluded.topic, status = excluded.status""",
                (
                    snapshot.board_id,
                    item.id,
                    item.revision,
                    item.kind,
                    item.topic,
                    item.lifecycle if isinstance(item, Fact) else item.status,
                ),
            )
        connection.execute(
            "INSERT INTO board_events (board_id, sequence, payload) VALUES (?, ?, ?)",
            (snapshot.board_id, event.sequence, event.model_dump_json()),
        )

    async def create(self, snapshot: BoardSnapshot, event: BoardEvent) -> BoardSnapshot:
        def create(connection: sqlite3.Connection) -> BoardSnapshot:
            if connection.execute(
                "SELECT 1 FROM boards WHERE board_id = ?", (snapshot.board_id,)
            ).fetchone():
                raise BlackboardError("board already exists")
            validate_publication(snapshot, event)
            connection.execute(
                "INSERT INTO boards (board_id, revision, root_intent_id) VALUES (?, ?, ?)",
                (snapshot.board_id, snapshot.revision, snapshot.root_intent_id),
            )
            connection.execute(
                "INSERT INTO board_control (board_id) VALUES (?)", (snapshot.board_id,)
            )
            self._publish_objects(connection, snapshot, event)
            return snapshot.model_copy(deep=True)

        return await self.transaction(create, write=True)

    async def get_object(self, board_id: str, ref: ObjectRef) -> BoardObject:
        def get(connection: sqlite3.Connection) -> BoardObject:
            row = connection.execute(
                """SELECT payload FROM board_object_revisions
                   WHERE board_id = ? AND object_id = ? AND revision = ?""",
                (board_id, ref.object_id, ref.revision),
            ).fetchone()
            if row is None:
                raise BlackboardError("reference is missing from this board")
            return _OBJECT.validate_json(row["payload"])

        return await self.transaction(get)

    @staticmethod
    def _receipt(connection: sqlite3.Connection, board_id: str, key: str) -> CommitReceipt | None:
        row = connection.execute(
            "SELECT payload FROM board_commits WHERE board_id = ? AND idempotency_key = ?",
            (board_id, key),
        ).fetchone()
        return CommitReceipt.model_validate_json(row["payload"]) if row else None

    async def receipt(self, board_id: str, key: str) -> CommitReceipt | None:
        return await self.transaction(lambda connection: self._receipt(connection, board_id, key))

    async def events(self, board_id: str, *, after: int = 0) -> tuple[BoardEvent, ...]:
        def events(connection: sqlite3.Connection) -> tuple[BoardEvent, ...]:
            self._board(connection, board_id)
            rows = connection.execute(
                """SELECT payload FROM board_events WHERE board_id = ? AND sequence > ?
                   ORDER BY sequence""",
                (board_id, after),
            ).fetchall()
            return tuple(BoardEvent.model_validate_json(row["payload"]) for row in rows)

        return await self.transaction(events)

    async def event_window(self, board_id: str, *, after: int = 0, limit: int = 100) -> EventWindow:
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError("invalid event window bounds")

        def read(connection: sqlite3.Connection) -> EventWindow:
            board = self._board(connection, board_id)
            first = connection.execute(
                "SELECT MIN(sequence) FROM board_events WHERE board_id = ?", (board_id,)
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT payload FROM board_events WHERE board_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (board_id, after, limit),
            ).fetchall()
            return EventWindow(
                board_id=board_id,
                revision=board["revision"],
                first_sequence=first,
                events=tuple(BoardEvent.model_validate_json(row["payload"]) for row in rows),
                finalization=self._control(connection, board_id).finalization,
            )

        return await self.transaction(read)

    async def object_history(
        self, board_id: str, object_id: str, *, through: int, after: int = 0, limit: int = 100
    ) -> tuple[ObjectRevision, ...]:
        if through < 1 or after < 0 or not 1 <= limit <= 200:
            raise ValueError("invalid history bounds")

        def read(connection: sqlite3.Connection) -> tuple[ObjectRevision, ...]:
            self._board(connection, board_id)
            rows = connection.execute(
                """SELECT r.payload AS object, e.payload AS event FROM board_object_revisions r
                   LEFT JOIN board_events e
                     ON e.board_id = r.board_id AND e.sequence = r.board_revision
                   WHERE r.board_id = ? AND r.object_id = ?
                     AND r.board_revision <= ? AND r.revision > ?
                   ORDER BY r.revision LIMIT ?""",
                (board_id, object_id, through, after, limit),
            ).fetchall()
            if any(row["event"] is None for row in rows):
                raise HistoryUnavailableError("object revision event is unavailable")
            return tuple(
                ObjectRevision(
                    object=_OBJECT.validate_json(row["object"]),
                    event=BoardEvent.model_validate_json(row["event"]),
                )
                for row in rows
            )

        return await self.transaction(read)

    async def commit(
        self,
        request: CommitRequest,
        *,
        writer: WriterToken,
    ) -> CommitReceipt:
        return await self.transaction(
            lambda connection: self.commit_in_transaction(connection, request, writer=writer),
            write=True,
        )

    def commit_in_transaction(
        self, connection: sqlite3.Connection, request: CommitRequest, *, writer: WriterToken
    ) -> CommitReceipt:
        """Publish inside the caller's transaction, including an activation settlement."""
        check_writer(
            self._control(connection, request.board_id), writer, self._process_lock.session_id
        )
        prior = self._receipt(connection, request.board_id, request.idempotency_key)
        if prior:
            if prior.request_digest != request.request_digest:
                raise IdempotencyConflictError("idempotency key reused with different content")
            return prior
        current = self._snapshot(connection, request.board_id)
        result = request.apply(current.model_copy(deep=True))
        validate_commit(request, result, current)
        snapshot, event, receipt = result.snapshot, result.event, result.receipt
        self._publish_objects(connection, snapshot, event)
        connection.execute(
            "UPDATE boards SET revision = ? WHERE board_id = ?",
            (snapshot.revision, snapshot.board_id),
        )
        connection.execute(
            """INSERT INTO board_commits
               (board_id, idempotency_key, board_revision, payload) VALUES (?, ?, ?, ?)""",
            (
                snapshot.board_id,
                receipt.idempotency_key,
                receipt.board_revision,
                receipt.model_dump_json(),
            ),
        )
        if request.wakeup:
            control = self._control(connection, request.board_id)
            self._save_control(
                connection, control.model_copy(update={"wakeup_revision": snapshot.revision})
            )
        return receipt.model_copy(deep=True)

    async def put_content(
        self, board_id: str, body: str, *, media_type: str, writer: WriterToken
    ) -> ContentRef:
        encoded = body.encode("utf-8")
        ref = ContentRef(
            content_digest=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
            media_type=media_type,
        )
        stored = StoredContent(ref=ref, body=body)

        def put(connection: sqlite3.Connection) -> ContentRef:
            check_writer(self._control(connection, board_id), writer, self._process_lock.session_id)
            row = connection.execute(
                "SELECT payload FROM board_contents WHERE board_id = ? AND content_digest = ?",
                (board_id, ref.content_digest),
            ).fetchone()
            if row is not None:
                if StoredContent.model_validate_json(row["payload"]) != stored:
                    raise BlackboardError(
                        "content digest already registered with different metadata"
                    )
                return ref
            connection.execute(
                "INSERT INTO board_contents (board_id, content_digest, payload) VALUES (?, ?, ?)",
                (board_id, ref.content_digest, stored.model_dump_json()),
            )
            return ref

        return await self.transaction(put, write=True)

    async def content(self, board_id: str, ref: ContentRef) -> StoredContent:
        def get(connection: sqlite3.Connection) -> StoredContent:
            row = connection.execute(
                "SELECT payload FROM board_contents WHERE board_id = ? AND content_digest = ?",
                (board_id, ref.content_digest),
            ).fetchone()
            if row is None:
                raise BlackboardError("content reference is missing from this board")
            stored = StoredContent.model_validate_json(row["payload"])
            if stored.ref != ref:
                raise BlackboardError("content reference is missing from this board")
            encoded = stored.body.encode("utf-8")
            if (
                len(encoded) != ref.size_bytes
                or hashlib.sha256(encoded).hexdigest() != ref.content_digest
            ):
                raise BlackboardError("stored content failed integrity check")
            return stored

        return await self.transaction(get)
