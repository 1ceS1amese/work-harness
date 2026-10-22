"""Reconcile durable knowledge and attempts before entering any saved graph task."""

from __future__ import annotations

import threading
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from work_harness.blackboard.controller import BLACKBOARD_WORKFLOW_REVISION, BlackboardController
from work_harness.blackboard.execution import DispatchItem
from work_harness.blackboard.protocol import BlackboardError
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore
from work_harness.workflow.blackboard_graph import build_blackboard_graph

_ACTIVE_RUNS: set[tuple[object, str]] = set()
_ACTIVE_LOCK = threading.Lock()


class BlackboardRunner:
    """Host entry point for fresh execution and checkpoint recovery.

    A new controller/runner instance is required when ownership changes. Its source services
    pin the new generation; an older in-flight instance never receives that authority.
    """

    def __init__(self, controller: BlackboardController, *, checkpointer: Any = None) -> None:
        self.controller = controller
        self.graph = build_blackboard_graph(controller, checkpointer=checkpointer)
        self.checkpointer = checkpointer

    async def run(
        self, board_id: str, config: RunnableConfig | None = None, *, takeover: bool = False
    ) -> dict[str, Any]:
        store = self.controller.kernel.store
        # SQLite handles in this process may share a database and writer generation.
        # A second runner must not mistake a live sibling for crashed work and replay it.
        namespace = (
            ("sqlite", str(store.path)) if isinstance(store, SQLiteBlackboardStore) else store
        )
        identity = (namespace, board_id)
        with _ACTIVE_LOCK:
            if identity in _ACTIVE_RUNS:
                raise BlackboardError("a runner is already executing this board")
            _ACTIVE_RUNS.add(identity)
        try:
            return await self._run(board_id, config, takeover=takeover)
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_RUNS.remove(identity)

    async def _run(
        self, board_id: str, config: RunnableConfig | None, *, takeover: bool
    ) -> dict[str, Any]:
        controller = self.controller
        checkpoint = await self.graph.aget_state(config) if self.checkpointer else None
        values = checkpoint.values if checkpoint else {}
        if values and values.get("board_id") != board_id:
            raise BlackboardError("checkpoint belongs to a different board")
        board = await controller.kernel.store.snapshot(board_id)
        if board.finalization is not None:
            await controller.reconcile(board_id)
            return {
                "board_id": board_id,
                "workflow_revision": BLACKBOARD_WORKFLOW_REVISION,
                **await controller.finish(
                    {
                        "board_id": board_id,
                        "status": board.finalization.status,
                        "stop_reason": board.finalization.reason,
                    }
                ),
            }
        if values.get("workflow_revision") not in (None, BLACKBOARD_WORKFLOW_REVISION):
            raise BlackboardError("checkpoint workflow revision is incompatible")
        if self.checkpointer:
            configurable = (config or {}).get("configurable", {})
            if configurable.get("checkpoint_id") or configurable.get("checkpoint_ns"):
                raise BlackboardError("recovery requires the current root checkpoint")
            await controller.ledger.start_run(
                board_id, controller.limits, now=controller.kernel.clock()
            )
            await controller.ledger.bind_thread(board_id, str(configurable["thread_id"]))
            checkpoint = await self.graph.aget_state(config, subgraphs=True)
        records = {r.candidate.id: r for r in await controller.ledger.records(board_id)}
        resumable: set[tuple[str, int]] = set()
        for task in checkpoint.tasks if checkpoint else ():
            if task.name in {
                "__start__",
                "bootstrap",
                "assess",
                "commit",
                "finish",
                "wait",
                "human",
            }:
                continue
            # Send task positions correspond to the persisted dispatch list. A child
            # checkpoint might not exist yet, so root's former single-activation fallback
            # is insufficient (especially when one source handles several Intents).
            path = task.path
            items = values.get("dispatch_items", [])
            if (
                len(path) < 2
                or path[0] != "__pregel_push"
                or type(path[1]) is not int
                or not 0 <= path[1] < len(items)
            ):
                raise BlackboardError("checkpoint has no explicit batch dispatch position")
            item = DispatchItem.model_validate(items[path[1]])
            identity, attempt = item.activation_id, item.attempt
            if item.source_id != task.name:
                raise BlackboardError("checkpoint source does not match its batch dispatch")
            child = getattr(task.state, "values", None)
            if child and (child.get("activation_id"), child.get("activation_attempt")) != (
                identity,
                attempt,
            ):
                raise BlackboardError("child checkpoint does not match its activation attempt")
            exact = await controller.ledger.get(identity, attempt)
            if (
                exact.candidate.board_id != board_id
                or exact.spec.id != item.source_id
                or exact.batch_id != values.get("batch_id")
            ):
                raise BlackboardError("checkpoint dispatch does not match the durable attempt")
            record = records.get(identity)
            if (
                record is not None
                and record.attempt == attempt
                and record.spec.id == task.name
                and (record.status == "running" or record.output is not None)
            ):
                resumable.add((identity, attempt))

        await controller.reconcile(board_id, resumable=frozenset(resumable), takeover=takeover)
        board = await controller.kernel.store.snapshot(board_id)
        if board.finalization is not None:
            return {
                "board_id": board_id,
                "workflow_revision": BLACKBOARD_WORKFLOW_REVISION,
                **await controller.finish(
                    {
                        "board_id": board_id,
                        "status": board.finalization.status,
                        "stop_reason": board.finalization.reason,
                    }
                ),
            }

        # Stopped Send tasks return a completion from the source wrapper; siblings retain
        # their original checkpoint namespaces and resume their completed private steps.
        control = await controller.kernel.store.control(board_id)
        if checkpoint and "human" in checkpoint.next:
            if control.wakeup_revision <= values.get("observed_revision", 0):
                return dict(values)
            input_value = Command(resume={"input_revision": control.wakeup_revision})
        else:
            input_value = None if checkpoint and checkpoint.next else {"board_id": board_id}
        return await self.graph.ainvoke(input_value, config)
