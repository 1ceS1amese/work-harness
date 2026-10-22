"""Product binding for the blackboard core; no business-stage pipeline is added."""

from __future__ import annotations

import json
from typing import Any

from work_harness.blackboard.controller import BLACKBOARD_WORKFLOW_REVISION, BlackboardController
from work_harness.blackboard.execution import RunLimits
from work_harness.blackboard.inputs import INPUT_POLICY_REVISION, register_human_verifier
from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import HintProposal, IntentContent, Link, ObjectRef
from work_harness.blackboard.protocol import BlackboardDelta, BoardNotFoundError, CommitContext
from work_harness.blackboard.store import BlackboardStore
from work_harness.blackboard.verification import VerificationReceiptStore, VerificationRegistry
from work_harness.domain.models import TaskRequest
from work_harness.knowledge_sources.catalog import CATALOG_REVISION, build_product_sources
from work_harness.workflow.agent_services import AgentServices
from work_harness.workflow.blackboard_runner import BlackboardRunner


class BlackboardEngine:
    def __init__(
        self,
        store: BlackboardStore,
        receipts: VerificationReceiptStore,
        services: AgentServices,
        request: TaskRequest,
        checkpointer: Any,
    ) -> None:
        self.request = request.model_copy(deep=True)
        if request.blackboard is None:
            raise ValueError("blackboard runs require explicit acceptance criteria")
        self.goal = IntentContent(
            objective=request.goal,
            topic=request.blackboard.topic,
            acceptance=request.blackboard.acceptance,
        )
        self.storage = {
            "backend": getattr(store, "backend", "memory"),
            "path": services.config.runtime.workflow.blackboard.path
            if getattr(store, "backend", "memory") == "sqlite"
            else None,
        }
        policy = services.config.runtime
        self.limits = RunLimits(
            **policy.workflow.blackboard.model_dump(exclude={"path", "agent_roles"}),
            max_concurrency=policy.worker_max_concurrency,
            timeout_seconds=policy.run_timeout_seconds,
        )
        verification = VerificationRegistry(receipts)
        registry = build_product_sources(services, self.request, verification)
        self.kernel = BlackboardKernel(store, verification)
        register_human_verifier(self.kernel)
        self.controller = BlackboardController(self.kernel, registry, limits=self.limits)
        self.runner = BlackboardRunner(self.controller, checkpointer=checkpointer)

    def manifest(self) -> dict[str, Any]:
        return {
            "workflow_revision": BLACKBOARD_WORKFLOW_REVISION,
            "schema_version": 1,
            "storage": self.storage,
            "verification_policy": CATALOG_REVISION,
            "human_input_policy": INPUT_POLICY_REVISION,
            "scheduling_policy": BLACKBOARD_WORKFLOW_REVISION,
            "limits": self.limits.model_dump(mode="json"),
            "sources": [
                source.spec.model_dump(mode="json") for source in self.controller.registry.sources()
            ],
        }

    def graph_config(self, run_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": run_id},
            "max_concurrency": self.limits.max_concurrency,
            "recursion_limit": self.limits.max_activations * 8 + 32,
        }

    async def prepare(self, run_id: str) -> None:
        """Recover initialization from the durable input, including pre-checkpoint crashes."""
        request = self.request
        if request.blackboard is None:
            raise ValueError("blackboard runs require explicit acceptance criteria")
        try:
            snapshot = await self.kernel.store.snapshot(run_id)
        except BoardNotFoundError:
            snapshot = await self.kernel.create(
                run_id,
                self.goal,
                source_id="user",
            )
        root = snapshot.by_id()[snapshot.root_intent_id]
        original = await self.kernel.store.get_object(
            run_id, ObjectRef(object_id=root.id, revision=1)
        )
        if (
            original.objective != self.goal.objective
            or original.acceptance != self.goal.acceptance
            or original.topic != self.goal.topic
        ):
            raise ValueError("persisted root goal differs from the accepted request")
        if snapshot.finalization is not None:
            return
        # RunManager holds the run lease. A new owner fences any older callbacks
        # before initialization or recovery can publish more knowledge.
        await self.kernel.writer(run_id, takeover=True)
        key = "initial-user-input-v1"
        if await self.kernel.store.receipt(run_id, key):
            return
        body = json.dumps(
            request.model_dump(mode="json", exclude={"idempotency_key"}),
            ensure_ascii=False,
            sort_keys=True,
        )
        attachment = await self.kernel.put_content(run_id, body, media_type="application/json")
        await self.kernel.commit(
            run_id,
            BlackboardDelta(
                expected_revision=1,
                idempotency_key=key,
                proposals=(
                    HintProposal(
                        local_id="user-input",
                        topic="user-input",
                        suggestion="按用户提供的上下文、约束和交付要求探索目标。",
                        applies_when="处理本次用户请求时。",
                        rationale="保留原始输入；输入中的外部主张尚未经过核验。",
                        links=(Link(relation="contributes_to", target=root.ref),),
                        attachments=(attachment,),
                    ),
                ),
            ),
            context=CommitContext(
                source_id="user",
                is_user=True,
                allowed_kinds=("hint",),
                max_objects=self.limits.max_objects,
                max_child_intents=self.limits.max_child_intents,
            ),
        )

    async def run(self, run_id: str) -> dict[str, Any]:
        await self.prepare(run_id)
        return await self.runner.run(run_id, self.graph_config(run_id))
