"""PostgreSQL blackboard with per-board writes and consistent read snapshots.

Every writer (including the activation ledger) takes the same transaction-scoped
lock before reading state. Connections are never held while an agent runs.
Queue-bound handles validate the live Job and Run on that same connection.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from copy import copy
from datetime import datetime
from typing import Any, TypeVar
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
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
    BoardClosedError,
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
    StaleWriterError,
    StoredContent,
    TerminalStatus,
    WriterToken,
)
from work_harness.blackboard.store import CommitRequest, validate_commit, validate_publication
from work_harness.domain.enums import RunStatus
from work_harness.storage.blackboard_postgres_migrations import (
    SCHEMA_VERSION,
    migrate,
    schema_version,
)
from work_harness.storage.postgres import PostgresRunStore, PostgresSchemaError
from work_harness.storage.queue import RunJobLease, RunLeaseClosedError, RunLeaseLostError

_Result = TypeVar("_Result")
_OBJECT = TypeAdapter(BoardObject)
Connection = AsyncConnection[dict[str, Any]]


async def one(connection: Connection, query: str, params: tuple = ()) -> dict | None:
    return await (await connection.execute(query, params)).fetchone()


async def all_rows(connection: Connection, query: str, params: tuple = ()) -> list[dict]:
    return await (await connection.execute(query, params)).fetchall()


class PostgresBlackboardStore:
    backend = "postgres"
    schema_version = SCHEMA_VERSION

    def __init__(self, dsn: str, *, pool_min_size: int = 1, pool_max_size: int = 10) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=pool_min_size,
            max_size=pool_max_size,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        self._opened = False
        self._initialized = False
        self._session_id = str(uuid4())
        self._lease: RunJobLease | None = None
        self._owns_pool = True
        self._connection: Connection | None = None

    def for_lease(
        self, lease: RunJobLease, *, connection: Connection | None = None
    ) -> PostgresBlackboardStore:
        """An immutable lease scope for one attempt; the root store owns the shared pool.

        Never replace the lease on a running handle: late callbacks must keep their
        original token even when another worker has already claimed the job.
        """
        if not self._initialized:
            raise BlackboardError("blackboard store must be initialized before use")
        scoped = copy(self)
        scoped._lease = RunJobLease.model_validate(lease.model_dump())
        scoped._session_id = str(uuid4())
        scoped._owns_pool = False
        scoped._connection = connection
        return scoped

    async def _open(self) -> None:
        if not self._opened:
            await self._pool.open(wait=True)
            self._opened = True

    async def initialize(self) -> None:
        await self._open()
        async with self._pool.connection() as connection:
            version = await schema_version(connection)
        if version != SCHEMA_VERSION:
            raise PostgresSchemaError(
                f"unsupported blackboard schema version: database={version} "
                f"application={SCHEMA_VERSION}; run `work-harness migrate`"
            )
        self._initialized = True

    async def setup(self) -> None:
        """Explicit migration entry point; initialize never changes the schema."""
        await self._open()
        async with self._pool.connection() as connection, connection.transaction():
            await migrate(connection)

    async def close(self) -> None:
        self._initialized = False
        if self._owns_pool:
            await self._pool.close()

    async def _check_lease(self, connection: Connection, *, lock: bool) -> None:
        if self._lease is None:
            return
        try:
            await PostgresRunStore.check_job_lease_in_transaction(
                connection, self._lease, lock=lock, engine="blackboard"
            )
        except RunLeaseClosedError as exc:
            raise BoardClosedError(str(exc)) from exc
        except RunLeaseLostError as exc:
            raise StaleWriterError(str(exc)) from exc

    async def _check_board_scope(
        self, connection: Connection, board_id: str, *, execution_write: bool
    ) -> None:
        if self._lease is not None and self._lease.run_id != board_id:
            raise StaleWriterError("lease belongs to a different board")
        row = await one(
            connection,
            "SELECT queue_managed FROM work_harness_boards WHERE board_id = %s",
            (board_id,),
        )
        if row is not None and execution_write:
            if row["queue_managed"] and self._lease is None:
                raise StaleWriterError("queue-managed blackboard requires a job lease")
            if not row["queue_managed"] and self._lease is not None:
                raise StaleWriterError("standalone blackboard cannot be adopted as a queued run")
            if row["queue_managed"]:
                control = await self._control(connection, board_id)
                if control.cancelled or control.finalization is not None:
                    raise BoardClosedError("blackboard is already closed")

    async def lock_for_write(
        self, connection: Connection, board_id: str, *, execution_write: bool = True
    ) -> None:
        """Called after the transaction's Job/Run locks, never before them."""
        await self.lock_board(connection, board_id)
        await self._check_board_scope(connection, board_id, execution_write=execution_write)

    @staticmethod
    async def lock_board(connection: Connection, board_id: str) -> None:
        # Include schema for isolated installations/tests; locks are already DB-scoped.
        # Hash collisions only serialize unrelated boards, never weaken exclusion.
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "current_schema() || ':work_harness:blackboard:' || %s, 0))",
            (board_id,),
        )

    async def transaction(
        self,
        operation: Callable[[Connection], Awaitable[_Result]],
        *,
        board_id: str | None = None,
        write: bool = False,
        execution_write: bool = True,
    ) -> _Result:
        if not self._initialized:
            raise BlackboardError("blackboard store must be initialized before use")
        if self._connection is not None:
            # Host input publication owns the surrounding Job/Run/Board transaction.
            # Nested kernel and verification operations must join it, never commit early.
            connection = self._connection
            if write:
                if execution_write:
                    await self._check_lease(connection, lock=True)
                if board_id is not None:
                    await self.lock_for_write(connection, board_id, execution_write=execution_write)
            result = await operation(connection)
            if write and execution_write:
                await self._check_lease(connection, lock=True)
            return result
        async with self._pool.connection() as connection, connection.transaction():
            if write:
                # READ COMMITTED sees the preceding writer after waiting for its lock.
                await connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                if execution_write:
                    await self._check_lease(connection, lock=True)
                if board_id is not None:
                    await self.lock_for_write(connection, board_id, execution_write=execution_write)
            else:
                # Board revision, objects, ledger and control must share one read snapshot.
                await connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
            result = await operation(connection)
            if write and execution_write:
                # Locks prevent takeover/cancel while publishing. Recheck DB time as
                # well so expiry during a long transaction rolls back all its writes.
                await self._check_lease(connection, lock=True)
            return result

    @staticmethod
    async def _control(connection: Connection, board_id: str) -> BoardControl:
        row = await one(
            connection, "SELECT * FROM work_harness_board_control WHERE board_id = %s", (board_id,)
        )
        if row is None:
            raise BoardNotFoundError(f"unknown board: {board_id}")
        return BoardControl(
            board_id=board_id,
            generation=row["generation"],
            owner_id=row["owner_id"],
            session_id=row["session_id"],
            cancelled=row["cancelled"],
            finalization=BoardFinalization.model_validate(row["finalization"])
            if row["finalization"]
            else None,
            wakeup_revision=row["wakeup_revision"],
            status_pending=row["status_pending"],
        )

    @staticmethod
    async def _save_control(connection: Connection, control: BoardControl) -> None:
        await connection.execute(
            """UPDATE work_harness_board_control SET generation = %s, owner_id = %s,
               session_id = %s, cancelled = %s, finalization = %s, wakeup_revision = %s,
               status_pending = %s WHERE board_id = %s""",
            (
                control.generation,
                control.owner_id,
                control.session_id,
                control.cancelled,
                Jsonb(control.finalization.model_dump(mode="json"))
                if control.finalization
                else None,
                control.wakeup_revision,
                control.status_pending,
                control.board_id,
            ),
        )

    async def control(self, board_id: str) -> BoardControl:
        return await self.transaction(lambda conn: self._control(conn, board_id))

    async def pending_reconciliations(self, *, limit: int = 200) -> tuple[str, ...]:
        async def read(connection: Connection) -> tuple[str, ...]:
            rows = await all_rows(
                connection,
                "SELECT board_id FROM work_harness_board_control WHERE wakeup_revision > 0 "
                "OR status_pending ORDER BY board_id LIMIT %s",
                (limit,),
            )
            return tuple(row["board_id"] for row in rows)

        return await self.transaction(read)

    async def release_writer(self, writer: WriterToken) -> None:
        async def release(connection: Connection) -> None:
            control = await self._control(connection, writer.board_id)
            await self._save_control(connection, release_writer(control, writer, writer.session_id))

        await self.transaction(release, board_id=writer.board_id, write=True, execution_write=False)

    async def acknowledge_finalization(self, board_id: str, revision: int) -> None:
        async def acknowledge(connection: Connection) -> None:
            control = await self._control(connection, board_id)
            if (
                control.finalization is None
                or control.finalization.finalized_at_revision != revision
            ):
                raise BlackboardError("finalization acknowledgment does not match the board")
            await self._save_control(
                connection, control.model_copy(update={"status_pending": False})
            )

        await self.transaction(acknowledge, board_id=board_id, write=True, execution_write=False)

    async def complete_job(self, *, observed_revision: int) -> bool:
        """Acknowledge a sealed result or a covered human wait with the queue atomically.

        False means new input arrived since the checkpoint observed its board: keep
        the lease and continue. A stale owner is never allowed to update the run index.
        """
        lease = self._lease
        if lease is None:
            raise StaleWriterError("job completion requires an execution lease")

        async def complete(connection: Connection) -> bool:
            await PostgresRunStore.check_job_lease_in_transaction(
                connection, lease, engine="blackboard", allow_reconciliation=True
            )
            await self.lock_for_write(connection, lease.run_id, execution_write=False)
            if not (await self._board(connection, lease.run_id))["queue_managed"]:
                raise BlackboardError("job completion requires a queue-managed board")
            control = await self._control(connection, lease.run_id)
            final = control.finalization
            if final is None:
                if observed_revision < 1:
                    raise BlackboardError("human wait requires an observed board revision")
                if control.wakeup_revision > observed_revision:
                    return False
                status = RunStatus.WAITING_HUMAN
            else:
                status = RunStatus(final.status)
            # Expiry is checked after all lock waits, on the same transaction connection.
            await PostgresRunStore.check_job_lease_in_transaction(
                connection, lease, engine="blackboard", allow_reconciliation=True
            )
            completed = await PostgresRunStore.complete_job_in_transaction(
                connection,
                lease.job_id,
                owner=lease.owner,
                lease_token=lease.token,
                run_status=status,
            )
            if not completed:
                raise StaleWriterError("job was replaced before completion")
            await self._save_control(
                connection,
                control.model_copy(
                    update={
                        "status_pending": False,
                        "wakeup_revision": 0,
                    }
                ),
            )
            return True

        return await self.transaction(complete, write=True, execution_write=False)

    async def acknowledge_wakeup(self, board_id: str, through: int, *, writer: WriterToken) -> None:
        async def acknowledge(connection: Connection) -> None:
            control = await self._control(connection, board_id)
            await self._check_writer(connection, board_id, writer)
            if control.wakeup_revision <= through:
                await self._save_control(
                    connection, control.model_copy(update={"wakeup_revision": 0})
                )

        await self.transaction(acknowledge, board_id=board_id, write=True)

    async def claim_writer(
        self, board_id: str, owner_id: str, *, takeover: bool = False
    ) -> WriterToken:
        async def claim(connection: Connection) -> WriterToken:
            control = await self._control(connection, board_id)
            # A different connection/session is not evidence that the previous worker died.
            if (
                control.finalization is None
                and not control.cancelled
                and control.owner_id is not None
                and control.session_id != self._session_id
                and not takeover
            ):
                raise StaleWriterError("blackboard already has a different writer session")
            updated, token = claim_writer(control, owner_id, self._session_id, takeover=takeover)
            await self._save_control(connection, updated)
            if self._lease is not None:
                await connection.execute(
                    "UPDATE work_harness_board_control SET queue_job_id = %s, "
                    "queue_lease_token = %s WHERE board_id = %s",
                    (self._lease.job_id, self._lease.token, board_id),
                )
            return token

        return await self.transaction(claim, board_id=board_id, write=True)

    async def check_writer(self, writer: WriterToken) -> None:
        await self.transaction(lambda conn: self.check_writer_in_transaction(conn, writer))

    async def check_writer_in_transaction(
        self, connection: Connection, writer: WriterToken
    ) -> None:
        await self._check_writer(connection, writer.board_id, writer)

    async def _check_writer(
        self, connection: Connection, board_id: str, writer: WriterToken
    ) -> None:
        await self._check_board_scope(connection, board_id, execution_write=True)
        await self._check_lease(connection, lock=False)
        check_writer(await self._control(connection, board_id), writer, writer.session_id)
        if self._lease is not None:
            row = await one(
                connection,
                "SELECT queue_job_id, queue_lease_token "
                "FROM work_harness_board_control WHERE board_id = %s",
                (board_id,),
            )
            if (row["queue_job_id"], row["queue_lease_token"]) != (
                self._lease.job_id,
                self._lease.token,
            ):
                raise StaleWriterError("blackboard writer belongs to an earlier job lease")

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
        async def finalize(connection: Connection) -> BoardFinalization:
            if status == "cancelled":
                board = await self._board(connection, board_id)
                if board["queue_managed"]:
                    # Host path: queue cancellation fences writers first. Its durable
                    # result is checked with Job -> Run -> Board locks before sealing.
                    await PostgresRunStore.check_cancelled_run_in_transaction(connection, board_id)
                await self.lock_for_write(connection, board_id, execution_write=False)
            control = await self._control(connection, board_id)
            if control.finalization is not None:
                return control.finalization
            if status != "cancelled":
                if writer is None:
                    raise BlackboardError("finalization requires a writer token")
                await self._check_writer(connection, board_id, writer)
                if control.wakeup_revision > (observed_revision or 0):
                    raise InputPendingError("new human input must be assessed before finalization")
            updated, event, final = seal_snapshot(
                await self._snapshot(connection, board_id),
                status,
                reason,
                clock(),
                attempted_directions,
            )
            await self._publish_objects(connection, updated, event)
            await connection.execute(
                "UPDATE work_harness_boards SET revision = %s WHERE board_id = %s",
                (updated.revision, board_id),
            )
            await self._save_control(connection, close_control(control, final))
            return final

        return await self.transaction(
            finalize,
            board_id=board_id if status != "cancelled" else None,
            write=True,
            execution_write=status != "cancelled",
        )

    @staticmethod
    async def _board(connection: Connection, board_id: str) -> dict:
        row = await one(
            connection, "SELECT * FROM work_harness_boards WHERE board_id = %s", (board_id,)
        )
        if row is None:
            raise BoardNotFoundError(f"unknown board: {board_id}")
        return row

    async def _snapshot(
        self, connection: Connection, board_id: str, revision: int | None = None
    ) -> BoardSnapshot:
        board = await self._board(connection, board_id)
        version = board["revision"] if revision is None else revision
        if not 1 <= version <= board["revision"]:
            raise BlackboardError("unknown board revision")
        if version == board["revision"]:
            rows = await all_rows(
                connection,
                """SELECT r.payload FROM work_harness_board_objects_current AS c JOIN
                   work_harness_board_object_revisions AS r USING (board_id, object_id,
                   revision) WHERE c.board_id = %s ORDER BY c.object_id""",
                (board_id,),
            )
        else:
            rows = await all_rows(
                connection,
                """SELECT DISTINCT ON (object_id) payload FROM
                   work_harness_board_object_revisions WHERE board_id = %s AND board_revision
                   <= %s ORDER BY object_id, revision DESC""",
                (board_id, version),
            )
        final = (await self._control(connection, board_id)).finalization
        return BoardSnapshot(
            board_id=board_id,
            revision=version,
            root_intent_id=board["root_intent_id"],
            objects=tuple(_OBJECT.validate_python(row["payload"]) for row in rows),
            finalization=final
            if final is not None and final.finalized_at_revision <= version
            else None,
        )

    async def snapshot(self, board_id: str, *, revision: int | None = None) -> BoardSnapshot:
        return await self.transaction(lambda conn: self._snapshot(conn, board_id, revision))

    @staticmethod
    async def _publish_objects(
        connection: Connection, snapshot: BoardSnapshot, event: BoardEvent
    ) -> None:
        changed = {ref.object_id for ref in event.changed_refs}
        for item in snapshot.objects:
            if item.id not in changed:
                continue
            await connection.execute(
                """INSERT INTO work_harness_board_object_revisions (board_id, object_id,
                   revision, board_revision, payload) VALUES (%s, %s, %s, %s, %s)""",
                (
                    snapshot.board_id,
                    item.id,
                    item.revision,
                    snapshot.revision,
                    Jsonb(item.model_dump(mode="json")),
                ),
            )
            await connection.execute(
                """INSERT INTO work_harness_board_objects_current (board_id, object_id,
                   revision, kind, topic, status) VALUES (%s, %s, %s, %s, %s, %s) ON
                   CONFLICT(board_id, object_id) DO UPDATE SET revision = excluded.revision,
                   kind = excluded.kind, topic = excluded.topic, status = excluded.status""",
                (
                    snapshot.board_id,
                    item.id,
                    item.revision,
                    item.kind,
                    item.topic,
                    item.lifecycle if isinstance(item, Fact) else item.status,
                ),
            )
        await connection.execute(
            """INSERT INTO work_harness_board_events (board_id, sequence, payload) VALUES
               (%s, %s, %s)""",
            (snapshot.board_id, event.sequence, Jsonb(event.model_dump(mode="json"))),
        )

    async def create(self, snapshot: BoardSnapshot, event: BoardEvent) -> BoardSnapshot:
        return await self.transaction(
            lambda connection: self.create_in_transaction(
                connection, snapshot, event, queue_managed=self._lease is not None
            ),
            board_id=snapshot.board_id,
            write=True,
        )

    async def create_in_transaction(
        self,
        connection: Connection,
        snapshot: BoardSnapshot,
        event: BoardEvent,
        *,
        queue_managed: bool,
    ) -> BoardSnapshot:
        """Caller owns the board lock and either a live lease or durable cancellation."""
        if await one(
            connection,
            "SELECT 1 FROM work_harness_boards WHERE board_id = %s",
            (snapshot.board_id,),
        ):
            raise BlackboardError("board already exists")
        validate_publication(snapshot, event)
        await connection.execute(
            """INSERT INTO work_harness_boards
               (board_id, revision, root_intent_id, queue_managed) VALUES (%s, %s, %s, %s)""",
            (snapshot.board_id, snapshot.revision, snapshot.root_intent_id, queue_managed),
        )
        await connection.execute(
            "INSERT INTO work_harness_board_control (board_id) VALUES (%s)",
            (snapshot.board_id,),
        )
        await self._publish_objects(connection, snapshot, event)
        return snapshot.model_copy(deep=True)

    async def get_object(self, board_id: str, ref: ObjectRef) -> BoardObject:
        async def get(connection: Connection) -> BoardObject:
            row = await one(
                connection,
                """SELECT payload FROM work_harness_board_object_revisions
                   WHERE board_id = %s AND object_id = %s AND revision = %s""",
                (board_id, ref.object_id, ref.revision),
            )
            if row is None:
                raise BlackboardError("reference is missing from this board")
            return _OBJECT.validate_python(row["payload"])

        return await self.transaction(get)

    @staticmethod
    async def _receipt(connection: Connection, board_id: str, key: str) -> CommitReceipt | None:
        row = await one(
            connection,
            """SELECT payload FROM work_harness_board_commits WHERE board_id = %s AND
               idempotency_key = %s""",
            (board_id, key),
        )
        return CommitReceipt.model_validate(row["payload"]) if row else None

    async def receipt(self, board_id: str, key: str) -> CommitReceipt | None:
        return await self.transaction(lambda conn: self._receipt(conn, board_id, key))

    async def events(self, board_id: str, *, after: int = 0) -> tuple[BoardEvent, ...]:
        async def read(connection: Connection) -> tuple[BoardEvent, ...]:
            await self._board(connection, board_id)
            rows = await all_rows(
                connection,
                """SELECT payload FROM work_harness_board_events WHERE board_id = %s AND
                   sequence > %s ORDER BY sequence""",
                (board_id, after),
            )
            return tuple(BoardEvent.model_validate(row["payload"]) for row in rows)

        return await self.transaction(read)

    async def event_window(self, board_id: str, *, after: int = 0, limit: int = 100) -> EventWindow:
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError("invalid event window bounds")

        async def read(connection: Connection) -> EventWindow:
            board = await self._board(connection, board_id)
            first = await one(
                connection,
                "SELECT MIN(sequence) AS first FROM work_harness_board_events WHERE board_id = %s",
                (board_id,),
            )
            rows = await all_rows(
                connection,
                """SELECT payload FROM work_harness_board_events WHERE board_id = %s AND
                   sequence > %s ORDER BY sequence LIMIT %s""",
                (board_id, after, limit),
            )
            return EventWindow(
                board_id=board_id,
                revision=board["revision"],
                first_sequence=first["first"],
                events=tuple(BoardEvent.model_validate(row["payload"]) for row in rows),
                finalization=(await self._control(connection, board_id)).finalization,
            )

        return await self.transaction(read)

    async def object_history(
        self, board_id: str, object_id: str, *, through: int, after: int = 0, limit: int = 100
    ) -> tuple[ObjectRevision, ...]:
        if through < 1 or after < 0 or not 1 <= limit <= 200:
            raise ValueError("invalid history bounds")

        async def read(connection: Connection) -> tuple[ObjectRevision, ...]:
            await self._board(connection, board_id)
            rows = await all_rows(
                connection,
                """SELECT r.payload AS object, e.payload AS event FROM
                   work_harness_board_object_revisions r LEFT JOIN work_harness_board_events
                   e ON e.board_id = r.board_id AND e.sequence = r.board_revision WHERE
                   r.board_id = %s AND r.object_id = %s AND r.board_revision <= %s AND
                   r.revision > %s ORDER BY r.revision LIMIT %s""",
                (board_id, object_id, through, after, limit),
            )
            if any(row["event"] is None for row in rows):
                raise HistoryUnavailableError("object revision event is unavailable")
            return tuple(
                ObjectRevision(
                    object=_OBJECT.validate_python(row["object"]),
                    event=BoardEvent.model_validate(row["event"]),
                )
                for row in rows
            )

        return await self.transaction(read)

    async def commit(self, request: CommitRequest, *, writer: WriterToken) -> CommitReceipt:
        return await self.transaction(
            lambda conn: self.commit_in_transaction(conn, request, writer=writer),
            board_id=request.board_id,
            write=True,
        )

    async def commit_in_transaction(
        self, connection: Connection, request: CommitRequest, *, writer: WriterToken
    ) -> CommitReceipt:
        """Caller must hold this board's write lock, including for activation settlement."""
        await self._check_writer(connection, request.board_id, writer)
        prior = await self._receipt(connection, request.board_id, request.idempotency_key)
        if prior:
            if prior.request_digest != request.request_digest:
                raise IdempotencyConflictError("idempotency key reused with different content")
            return prior
        current = await self._snapshot(connection, request.board_id)
        result = request.apply(current.model_copy(deep=True))
        validate_commit(request, result, current)
        snapshot, event, receipt = result.snapshot, result.event, result.receipt
        await self._publish_objects(connection, snapshot, event)
        await connection.execute(
            "UPDATE work_harness_boards SET revision = %s WHERE board_id = %s",
            (snapshot.revision, snapshot.board_id),
        )
        await connection.execute(
            """INSERT INTO work_harness_board_commits (board_id, idempotency_key,
               board_revision, payload) VALUES (%s, %s, %s, %s)""",
            (
                snapshot.board_id,
                receipt.idempotency_key,
                receipt.board_revision,
                Jsonb(receipt.model_dump(mode="json")),
            ),
        )
        if request.wakeup:
            control = await self._control(connection, request.board_id)
            await self._save_control(
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

        async def put(connection: Connection) -> ContentRef:
            await self._check_writer(connection, board_id, writer)
            row = await one(
                connection,
                """SELECT payload FROM work_harness_board_contents WHERE board_id = %s AND
                   content_digest = %s""",
                (board_id, ref.content_digest),
            )
            if row is not None:
                if StoredContent.model_validate(row["payload"]) != stored:
                    raise BlackboardError(
                        "content digest already registered with different metadata"
                    )
                return ref
            await connection.execute(
                """INSERT INTO work_harness_board_contents (board_id, content_digest,
                   payload) VALUES (%s, %s, %s)""",
                (board_id, ref.content_digest, Jsonb(stored.model_dump(mode="json"))),
            )
            return ref

        return await self.transaction(put, board_id=board_id, write=True)

    async def content(self, board_id: str, ref: ContentRef) -> StoredContent:
        async def get(connection: Connection) -> StoredContent:
            row = await one(
                connection,
                """SELECT payload FROM work_harness_board_contents WHERE board_id = %s AND
                   content_digest = %s""",
                (board_id, ref.content_digest),
            )
            if row is None:
                raise BlackboardError("content reference is missing from this board")
            stored = StoredContent.model_validate(row["payload"])
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
