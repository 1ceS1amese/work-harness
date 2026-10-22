"""Activation settlement and verification receipts in the PostgreSQL board transaction."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from psycopg.types.json import Jsonb

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
from work_harness.storage.blackboard_postgres import (
    Connection,
    PostgresBlackboardStore,
    all_rows,
    one,
)

_Result = TypeVar("_Result")


class PostgresVerificationReceiptStore:
    def __init__(self, database: PostgresBlackboardStore) -> None:
        self.database = database

    async def put(self, board_id: str, receipt: Verification) -> None:
        async def put(connection: Connection) -> None:
            row = await one(
                connection,
                """SELECT payload FROM work_harness_board_verification_receipts WHERE
                   board_id = %s AND receipt_id = %s""",
                (board_id, receipt.receipt_id),
            )
            if row is not None:
                if Verification.model_validate(row["payload"]) != receipt:
                    raise VerificationError("verification receipt is immutable")
                return
            if not await one(
                connection, "SELECT 1 FROM work_harness_boards WHERE board_id = %s", (board_id,)
            ):
                raise VerificationError("unknown board for verification receipt")
            await connection.execute(
                """INSERT INTO work_harness_board_verification_receipts (board_id,
                   receipt_id, payload) VALUES (%s, %s, %s)""",
                (board_id, receipt.receipt_id, Jsonb(receipt.model_dump(mode="json"))),
            )

        await self.database.transaction(put, board_id=board_id, write=True)

    async def get(self, board_id: str, receipt_id: str) -> Verification | None:
        async def get(connection: Connection) -> Verification | None:
            row = await one(
                connection,
                """SELECT payload FROM work_harness_board_verification_receipts WHERE
                   board_id = %s AND receipt_id = %s""",
                (board_id, receipt_id),
            )
            return Verification.model_validate(row["payload"]) if row else None

        return await self.database.transaction(get)


class PostgresActivationStore(ActivationStore):
    def __init__(self, database: PostgresBlackboardStore) -> None:
        self.database = database

    def bind(self, store: BlackboardStore) -> None:
        if store is not self.database:
            raise BlackboardError("activation ledger must share the blackboard PostgreSQL store")

    async def reconcile_terminal(self, board_id: str) -> bool:
        return await self.database.transaction(
            lambda connection: self.reconcile_terminal_in_transaction(connection, board_id),
            board_id=board_id,
            write=True,
            execution_write=False,
        )

    async def reconcile_terminal_in_transaction(
        self, connection: Connection, board_id: str
    ) -> bool:
        """Caller already holds the board lock; terminal cleanup joins its transaction."""
        control = await self.database._control(connection, board_id)
        if control.finalization is None:
            raise BlackboardError("terminal reconciliation requires a sealed blackboard")
        state = await self._load(connection, board_id)
        previous_execution = state.execution
        previous_attempts, previous_calls = dict(state.attempts), dict(state.calls)
        now = control.finalization.finalized_at
        for record in state.records():
            if record.status == "running":
                state.stop(record.ref, reason="board_closed", retry=False, now=now)
            elif record.status == "result_ready":
                # PostgreSQL publication and completion have always been atomic.
                # A still-pending output therefore has no accepted publication.
                state.finish(
                    record.candidate.id,
                    record.attempt,
                    "rejected",
                    "board_closed",
                    False,
                    0,
                    now,
                )
        if state.execution is not None:
            state.execution = state.execution.model_copy(update={"wakeup": None})
        await self._save(connection, state, previous_execution, previous_attempts, previous_calls)
        return True

    async def commit_activation(
        self,
        request: CommitRequest,
        ref: ActivationRef,
        writer: WriterToken,
        clock: Callable[[], datetime],
        specs: tuple[KnowledgeSourceSpec, ...] = (),
    ) -> CommitReceipt:
        async def commit(connection: Connection) -> CommitReceipt:
            await self.database.check_writer_in_transaction(connection, writer)
            state = await self._load(connection, request.board_id)
            record = state.validate_settlement(request, ref, writer)
            if (
                record.status != "result_ready"
                and await self.database._receipt(
                    connection, request.board_id, request.idempotency_key
                )
                is None
            ):
                raise BlackboardError("completed activation has no commit receipt")
            previous_execution = state.execution
            previous_attempts, previous_calls = dict(state.attempts), dict(state.calls)
            before = await self.database._snapshot(connection, request.board_id)
            receipt = await self.database.commit_in_transaction(connection, request, writer=writer)
            after = await self.database._snapshot(connection, request.board_id)
            if before.revision >= receipt.board_revision:
                before = await self.database._snapshot(
                    connection, request.board_id, receipt.board_revision - 1
                )
                after = await self.database._snapshot(
                    connection, request.board_id, receipt.board_revision
                )
            state.settle(request, ref, writer, receipt, receipt.committed_at, before, after, specs)
            await self._save(
                connection, state, previous_execution, previous_attempts, previous_calls
            )
            return receipt

        return await self.database.transaction(commit, board_id=request.board_id, write=True)

    async def capture_progress(
        self,
        board_id: str,
        specs: tuple[KnowledgeSourceSpec, ...],
        writer: WriterToken,
        clock: Callable[[], datetime],
    ) -> ProgressState:
        async def capture(connection: Connection) -> ProgressState:
            await self.database.check_writer_in_transaction(connection, writer)
            if writer.board_id != board_id:
                raise BlackboardError("writer belongs to a different board")
            state = await self._load(connection, board_id)
            previous_execution = state.execution
            previous_attempts, previous_calls = dict(state.attempts), dict(state.calls)
            result = state.observe_progress(
                await self.database._snapshot(connection, board_id), specs, clock()
            )
            await self._save(
                connection, state, previous_execution, previous_attempts, previous_calls
            )
            return result

        return await self.database.transaction(capture, board_id=board_id, write=True)

    @staticmethod
    async def _load(connection: Connection, board_id: str) -> LedgerState:
        if not await one(
            connection, "SELECT 1 FROM work_harness_boards WHERE board_id = %s", (board_id,)
        ):
            raise BlackboardError("unknown board for activation ledger")
        state = LedgerState(board_id)
        row = await one(
            connection,
            "SELECT payload FROM work_harness_board_control WHERE board_id = %s",
            (board_id,),
        )
        if row and row["payload"] is not None:
            state.execution = RunExecution.model_validate(row["payload"])
        for row in await all_rows(
            connection,
            """SELECT payload FROM work_harness_board_activation_attempts WHERE board_id = %s
               ORDER BY ordinal""",
            (board_id,),
        ):
            record = ActivationRecord.model_validate(row["payload"])
            state.attempts[(record.candidate.id, record.attempt)] = record
        for row in await all_rows(
            connection,
            """SELECT u.activation_id, u.attempt, u.call_id, u.payload FROM
               work_harness_board_usage AS u JOIN work_harness_board_activations AS a USING
               (activation_id) WHERE a.board_id = %s""",
            (board_id,),
        ):
            state.calls[(row["activation_id"], row["attempt"], row["call_id"])] = (
                UsageRecord.model_validate(row["payload"])
            )
        return state

    @staticmethod
    async def _save(
        connection: Connection,
        state: LedgerState,
        previous_execution: RunExecution | None,
        previous_attempts: dict[tuple[str, int], ActivationRecord],
        previous_calls: dict[tuple[str, int, str], UsageRecord],
    ) -> None:
        if state.execution is not None and state.execution != previous_execution:
            await connection.execute(
                "UPDATE work_harness_board_control SET payload = %s WHERE board_id = %s",
                (Jsonb(state.execution.model_dump(mode="json")), state.board_id),
            )
        for key, record in state.attempts.items():
            if record == previous_attempts.get(key):
                continue
            await connection.execute(
                """INSERT INTO work_harness_board_activations (activation_id, board_id)
                   VALUES (%s, %s) ON CONFLICT(activation_id) DO NOTHING""",
                (record.candidate.id, state.board_id),
            )
            await connection.execute(
                """INSERT INTO work_harness_board_activation_attempts (activation_id,
                   attempt, board_id, ordinal, payload) VALUES (%s, %s, %s, %s, %s) ON
                   CONFLICT(activation_id, attempt) DO UPDATE SET payload = excluded.payload""",
                (
                    record.candidate.id,
                    record.attempt,
                    state.board_id,
                    record.ordinal,
                    Jsonb(record.model_dump(mode="json")),
                ),
            )
        for key, usage in state.calls.items():
            if previous_calls.get(key) != usage:
                await connection.execute(
                    """INSERT INTO work_harness_board_usage (activation_id, attempt, call_id,
                       payload) VALUES (%s, %s, %s, %s)
                       ON CONFLICT(activation_id, attempt, call_id)
                       DO UPDATE SET payload = EXCLUDED.payload""",
                    (*key, Jsonb(usage.model_dump(mode="json"))),
                )

    async def _transaction(
        self,
        operation: Callable[[LedgerState], _Result],
        *,
        board_id: str | None = None,
        activation_id: str | None = None,
        write: bool = False,
        writer: WriterToken | None = None,
        execution_write: bool = True,
    ) -> _Result:
        async def transact(connection: Connection) -> _Result:
            selected_board = board_id
            if activation_id is not None:
                row = await one(
                    connection,
                    "SELECT board_id FROM work_harness_board_activations WHERE activation_id = %s",
                    (activation_id,),
                )
                selected_board = row["board_id"] if row else None
            if selected_board is None:
                raise BlackboardError("unknown activation attempt")
            # Activation-to-board identity is immutable. Resolve it before taking the lock,
            # then read the ledger under READ COMMITTED after the previous writer finishes.
            if write:
                await self.database.lock_for_write(
                    connection, selected_board, execution_write=execution_write
                )
            if writer is not None:
                if writer.board_id != selected_board:
                    raise BlackboardError("writer belongs to a different board")
                await self.database.check_writer_in_transaction(connection, writer)
            state = await self._load(connection, selected_board)
            previous_execution = state.execution
            previous_attempts, previous_calls = dict(state.attempts), dict(state.calls)
            result = operation(state)
            if write:
                await self._save(
                    connection, state, previous_execution, previous_attempts, previous_calls
                )
            return result

        return await self.database.transaction(
            transact, write=write, execution_write=execution_write
        )

    async def _usage_transaction(
        self, operation: Callable[[LedgerState], _Result], *, activation_id: str
    ) -> _Result:
        # This narrow path only accounts for an existing attempt. It cannot reserve,
        # save output, publish objects, finish an activation or change writer ownership.
        return await self._transaction(
            operation, activation_id=activation_id, write=True, execution_write=False
        )
