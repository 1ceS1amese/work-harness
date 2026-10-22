"""Repair only durable terminal decisions; never grant permission to execute agents."""

from __future__ import annotations

from work_harness.blackboard.control import close_control, seal_snapshot
from work_harness.blackboard.kernel import initial_board
from work_harness.blackboard.models import IntentContent
from work_harness.blackboard.protocol import BlackboardError
from work_harness.storage.blackboard_execution_postgres import PostgresActivationStore
from work_harness.storage.blackboard_postgres import Connection, PostgresBlackboardStore, one


async def reconcile_terminal(
    database: PostgresBlackboardStore, run_id: str, *, accepted_goal: IntentContent
) -> bool:
    async def reconcile(connection: Connection) -> bool:
        # The same Job -> Run -> Board lock order as publication and cancellation.
        await connection.execute(
            "SELECT job_id FROM work_harness_run_jobs WHERE run_id = %s ORDER BY job_id FOR UPDATE",
            (run_id,),
        )
        run = await one(
            connection,
            "SELECT status, execution, cancel_requested_at FROM work_harness_run_index "
            "WHERE run_id = %s FOR UPDATE",
            (run_id,),
        )
        if run is None or (run["execution"] or {}).get("engine") != "blackboard":
            raise BlackboardError("reconciliation requires an existing blackboard run")
        await database.lock_for_write(connection, run_id, execution_write=False)
        board = await one(
            connection,
            "SELECT queue_managed FROM work_harness_boards WHERE board_id = %s",
            (run_id,),
        )
        cancelled = run["cancel_requested_at"] is not None or run["status"] == "cancelled"
        now = (await one(connection, "SELECT clock_timestamp() AS now"))["now"]
        if board is None:
            if not cancelled:
                return False
            snapshot, event = initial_board(run_id, accepted_goal, source_id="user", now=now)
            await database.create_in_transaction(connection, snapshot, event, queue_managed=True)
        elif not board["queue_managed"]:
            raise BlackboardError("queue reconciliation cannot adopt a standalone board")
        control = await database._control(connection, run_id)
        if control.finalization is None:
            if not cancelled:
                return False
            updated, event, final = seal_snapshot(
                await database._snapshot(connection, run_id),
                "cancelled",
                "user_cancelled",
                now,
                (),
            )
            await database._publish_objects(connection, updated, event)
            await connection.execute(
                "UPDATE work_harness_boards SET revision = %s WHERE board_id = %s",
                (updated.revision, run_id),
            )
            control = close_control(control, final)
            await database._save_control(connection, control)
        final = control.finalization
        # No caller-supplied status can overwrite the immutable board result.
        await PostgresActivationStore(database).reconcile_terminal_in_transaction(
            connection, run_id
        )
        await connection.execute(
            "UPDATE work_harness_run_jobs SET status = %s, lease_owner = NULL, "
            "lease_token = NULL, lease_expires_at = NULL, completed_at = clock_timestamp(), "
            "updated_at = clock_timestamp() WHERE run_id = %s "
            "AND status IN ('available', 'leased', 'dead')",
            ("cancelled" if final.status == "cancelled" else "completed", run_id),
        )
        await connection.execute(
            "UPDATE work_harness_run_index SET status = %s, lease_owner = NULL, "
            "lease_expires_at = NULL, updated_at = clock_timestamp() WHERE run_id = %s",
            (final.status, run_id),
        )
        await database._save_control(
            connection, control.model_copy(update={"status_pending": False, "wakeup_revision": 0})
        )
        return True

    return await database.transaction(reconcile, write=True, execution_write=False)
