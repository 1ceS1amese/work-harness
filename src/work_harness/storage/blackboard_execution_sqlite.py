"""Durable activation accounting and verification receipts in the blackboard database."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from work_harness.blackboard.execution import (
    ActivationRecord,
    ActivationRef,
    ActivationStore,
    LedgerState,
    RunExecution,
)
from work_harness.blackboard.models import Verification
from work_harness.blackboard.progress import ProgressState
from work_harness.blackboard.protocol import (
    BlackboardError,
    CommitReceipt,
    VerificationError,
    WriterToken,
)
from work_harness.blackboard.store import BlackboardStore, CommitRequest
from work_harness.knowledge_sources.base import KnowledgeSourceSpec, UsageRecord
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore

_Result = TypeVar("_Result")


class SQLiteVerificationReceiptStore:
    def __init__(self, database: SQLiteBlackboardStore) -> None:
        self.database = database

    async def put(self, board_id: str, receipt: Verification) -> None:
        def put(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT payload FROM board_verification_receipts "
                "WHERE board_id = ? AND receipt_id = ?",
                (board_id, receipt.receipt_id),
            ).fetchone()
            if row is not None:
                if Verification.model_validate_json(row["payload"]) != receipt:
                    raise VerificationError("verification receipt is immutable")
                return
            if not connection.execute(
                "SELECT 1 FROM boards WHERE board_id = ?", (board_id,)
            ).fetchone():
                raise VerificationError("unknown board for verification receipt")
            connection.execute(
                "INSERT INTO board_verification_receipts (board_id, receipt_id, payload) "
                "VALUES (?, ?, ?)",
                (board_id, receipt.receipt_id, receipt.model_dump_json()),
            )

        await self.database.transaction(put, write=True)

    async def get(self, board_id: str, receipt_id: str) -> Verification | None:
        def get(connection: sqlite3.Connection) -> Verification | None:
            row = connection.execute(
                "SELECT payload FROM board_verification_receipts "
                "WHERE board_id = ? AND receipt_id = ?",
                (board_id, receipt_id),
            ).fetchone()
            return Verification.model_validate_json(row["payload"]) if row else None

        return await self.database.transaction(get)


class SQLiteActivationStore(ActivationStore):
    def __init__(self, database: SQLiteBlackboardStore) -> None:
        self.database = database

    def bind(self, store: BlackboardStore) -> None:
        if not isinstance(store, SQLiteBlackboardStore) or store.path != self.database.path:
            raise BlackboardError("activation ledger must share the blackboard SQLite database")

    async def commit_activation(
        self,
        request: CommitRequest,
        ref: ActivationRef,
        writer: WriterToken,
        clock: Callable[[], datetime],
        specs: tuple[KnowledgeSourceSpec, ...] = (),
    ) -> CommitReceipt:
        def commit(connection: sqlite3.Connection) -> CommitReceipt:
            self.database.check_writer_in_transaction(connection, writer)
            state = self._load(connection, request.board_id)
            record = state.validate_settlement(request, ref, writer)
            if (
                record.status != "result_ready"
                and self.database._receipt(connection, request.board_id, request.idempotency_key)
                is None
            ):
                raise BlackboardError("completed activation has no commit receipt")
            previous_execution = state.execution
            previous_attempts, previous_calls = dict(state.attempts), dict(state.calls)
            before = self.database._snapshot(connection, request.board_id)
            receipt = self.database.commit_in_transaction(connection, request, writer=writer)
            after = self.database._snapshot(connection, request.board_id)
            if before.revision >= receipt.board_revision:
                before = self.database._snapshot(
                    connection, request.board_id, receipt.board_revision - 1
                )
                after = self.database._snapshot(
                    connection, request.board_id, receipt.board_revision
                )
            state.settle(request, ref, writer, receipt, receipt.committed_at, before, after, specs)
            self._save(connection, state, previous_execution, previous_attempts, previous_calls)
            return receipt

        return await self.database.transaction(commit, write=True)

    async def capture_progress(
        self,
        board_id: str,
        specs: tuple[KnowledgeSourceSpec, ...],
        writer: WriterToken,
        clock: Callable[[], datetime],
    ) -> ProgressState:
        def capture(connection: sqlite3.Connection) -> ProgressState:
            self.database.check_writer_in_transaction(connection, writer)
            if writer.board_id != board_id:
                raise BlackboardError("writer belongs to a different board")
            state = self._load(connection, board_id)
            previous_execution = state.execution
            previous_attempts, previous_calls = dict(state.attempts), dict(state.calls)
            result = state.observe_progress(
                self.database._snapshot(connection, board_id), specs, clock()
            )
            self._save(connection, state, previous_execution, previous_attempts, previous_calls)
            return result

        return await self.database.transaction(capture, write=True)

    @staticmethod
    def _load(connection: sqlite3.Connection, board_id: str) -> LedgerState:
        if not connection.execute(
            "SELECT 1 FROM boards WHERE board_id = ?", (board_id,)
        ).fetchone():
            raise BlackboardError("unknown board for activation ledger")
        state = LedgerState(board_id)
        row = connection.execute(
            "SELECT payload FROM board_control WHERE board_id = ?", (board_id,)
        ).fetchone()
        if row and row["payload"] is not None:
            state.execution = RunExecution.model_validate_json(row["payload"])
        for row in connection.execute(
            "SELECT payload FROM board_activation_attempts WHERE board_id = ? ORDER BY ordinal",
            (board_id,),
        ):
            record = ActivationRecord.model_validate_json(row["payload"])
            state.attempts[(record.candidate.id, record.attempt)] = record
        for row in connection.execute(
            """SELECT u.activation_id, u.attempt, u.call_id, u.payload FROM board_usage AS u
               JOIN board_activations AS a USING (activation_id) WHERE a.board_id = ?""",
            (board_id,),
        ):
            state.calls[(row["activation_id"], row["attempt"], row["call_id"])] = (
                UsageRecord.model_validate_json(row["payload"])
            )
        return state

    @staticmethod
    def _save(
        connection: sqlite3.Connection,
        state: LedgerState,
        previous_execution: RunExecution | None,
        previous_attempts: dict[tuple[str, int], ActivationRecord],
        previous_calls: dict[tuple[str, int, str], UsageRecord],
    ) -> None:
        if state.execution is not None and state.execution != previous_execution:
            connection.execute(
                """INSERT INTO board_control (board_id, payload) VALUES (?, ?)
                   ON CONFLICT(board_id) DO UPDATE SET payload = excluded.payload""",
                (state.board_id, state.execution.model_dump_json()),
            )
        for key, record in state.attempts.items():
            if record == previous_attempts.get(key):
                continue
            connection.execute(
                "INSERT OR IGNORE INTO board_activations (activation_id, board_id) VALUES (?, ?)",
                (record.candidate.id, state.board_id),
            )
            connection.execute(
                """INSERT INTO board_activation_attempts
                   (activation_id, attempt, board_id, ordinal, payload) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(activation_id, attempt) DO UPDATE SET payload = excluded.payload""",
                (
                    record.candidate.id,
                    record.attempt,
                    state.board_id,
                    record.ordinal,
                    record.model_dump_json(),
                ),
            )
        for key, usage in state.calls.items():
            if previous_calls.get(key) != usage:
                connection.execute(
                    "INSERT INTO board_usage (activation_id, attempt, call_id, payload) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(activation_id, attempt, call_id) "
                    "DO UPDATE SET payload = excluded.payload",
                    (*key, usage.model_dump_json()),
                )

    async def _transaction(
        self,
        operation: Callable[[LedgerState], _Result],
        *,
        board_id: str | None = None,
        activation_id: str | None = None,
        write: bool = False,
        writer: WriterToken | None = None,
    ) -> _Result:
        def transact(connection: sqlite3.Connection) -> _Result:
            if writer is not None:
                self.database.check_writer_in_transaction(connection, writer)
            selected_board = board_id
            if activation_id is not None:
                row = connection.execute(
                    "SELECT board_id FROM board_activations WHERE activation_id = ?",
                    (activation_id,),
                ).fetchone()
                selected_board = row["board_id"] if row else None
            if selected_board is None:
                raise BlackboardError("unknown activation attempt")
            if writer is not None and writer.board_id != selected_board:
                raise BlackboardError("writer belongs to a different board")
            state = self._load(connection, selected_board)
            previous_execution = state.execution
            previous_attempts = dict(state.attempts)
            previous_calls = dict(state.calls)
            result = operation(state)
            if write:
                self._save(connection, state, previous_execution, previous_attempts, previous_calls)
            return result

        return await self.database.transaction(transact, write=write)
