"""State-driven source traces, bounded control and existing Agent interoperability."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from work_harness.blackboard.controller import BlackboardController
from work_harness.blackboard.execution import RunLimits
from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import (
    AcceptanceCriterion,
    Evidence,
    Fact,
    FactContent,
    FactProposal,
    Hint,
    HintProposal,
    Intent,
    IntentContent,
    IntentProposal,
    Link,
    digest,
)
from work_harness.blackboard.protocol import BlackboardDelta, CommitContext, ObjectChange
from work_harness.blackboard.verification import VerificationRegistry, Verifier
from work_harness.knowledge_sources.adapters.agent import AgentKnowledgeSource
from work_harness.knowledge_sources.adapters.callback import (
    ObservationKnowledgeSource,
    RuleKnowledgeSource,
    VerificationKnowledgeSource,
    VerifiedObservation,
)
from work_harness.knowledge_sources.base import (
    ActivationRule,
    BoardChangeSet,
    BoardView,
    FactQuery,
    KnowledgeOutput,
    KnowledgeSourceSpec,
    SourceLimits,
    UsageRecord,
)
from work_harness.knowledge_sources.execution import Invocation
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.llm.mock import MockProvider
from work_harness.observability.trace import TraceRecorder
from work_harness.runtime import build_runtime
from work_harness.storage.blackboard_memory import MemoryBlackboardStore
from work_harness.workflow.agent_services import AgentServices
from work_harness.workflow.blackboard_graph import build_blackboard_graph

ROOT = Path(__file__).resolve().parents[1]


def verify_observation(content: FactContent, proof: Any) -> tuple[str, ...]:
    if content.value != proof["value"] or content.claim_key != proof["key"]:
        raise ValueError("observation does not prove the proposed value")
    if content.evidence[0].content_digest != digest(proof):
        raise ValueError("evidence digest mismatch")
    if proof.get("operation") == "double" and proof["value"] != 2 * proof["operand"]:
        raise ValueError("invalid arithmetic derivation")
    return ("checked",)


async def setup_board(*, goal_type: str = "derived") -> BlackboardKernel:
    verification = VerificationRegistry()
    verification.register(
        Verifier(
            "observed",
            "1",
            "deterministic-test-observation",
            ("tool_observation", "derived", "delivery"),
            verify_observation,
        )
    )
    kernel = BlackboardKernel(MemoryBlackboardStore(), verification)
    await kernel.create(
        "board",
        IntentContent(
            topic="result",
            objective="给出可验证的结果",
            acceptance=(
                AcceptanceCriterion(
                    id="answer",
                    description="交付已验证的结果",
                    claim_key="answer",
                    claim_type=goal_type,
                    operator="equals" if goal_type == "derived" else "exists",
                    expected=42 if goal_type == "derived" else None,
                    required_checks=("checked",),
                ),
            ),
        ),
        source_id="user",
    )
    return kernel


def observation(
    view: BoardView,
    *,
    key: str,
    value: Any,
    topic: str = "input",
    claim_type: str = "tool_observation",
    premises: tuple = (),
    proof_extra: dict[str, Any] | None = None,
) -> VerifiedObservation:
    proof = {"key": key, "value": value, **(proof_extra or {})}
    content = FactContent(
        topic=topic,
        statement=f"已验证 {key} = {value!r}",
        claim_key=key,
        value=value,
        claim_type=claim_type,
        scope={"evaluation": "contract-test"},
        premise_refs=premises,
        evidence=(
            Evidence(
                source="rule" if premises else "tool",
                locator=f"test:{key}",
                content_digest=digest(proof),
                description="确定性测试证据",
            ),
        ),
        links=(Link(relation="contributes_to", target=view.intent.ref),),
    )
    return VerifiedObservation(local_id=key, content=content, verifier_id="observed", proof=proof)


def spec(
    name: str,
    *,
    rule: ActivationRule | None = None,
    emitted: tuple = ("hint",),
    topics: tuple = (),
    kind: str = "rule",
    limits: SourceLimits | None = None,
    model: str | None = None,
) -> KnowledgeSourceSpec:
    return KnowledgeSourceSpec(
        id=name,
        version="1",
        input_topics=topics,
        emitted_kinds=emitted,
        activation_rule=rule or ActivationRule(reason="处理当前目标"),
        adapter_type=kind,
        limits=limits or SourceLimits(),
        model_route=model,
    )


def pipeline() -> KnowledgeSourceRegistry:
    registry = KnowledgeSourceRegistry()

    async def observe(invocation: Invocation, view: BoardView) -> tuple[VerifiedObservation, ...]:
        return (observation(view, key="operand", value=21),)

    async def explore(invocation: Invocation, view: BoardView) -> KnowledgeOutput:
        return KnowledgeOutput(
            proposals=(
                HintProposal(
                    local_id="strategy",
                    topic="strategy",
                    suggestion="验证输入值的两倍是否满足目标。",
                    applies_when="已有 operand 观察",
                    rationale="当前目标需要一个可验证的数值推导。",
                    links=(Link(relation="contributes_to", target=view.intent.ref),),
                    derived_from=invocation.record.candidate.input_refs,
                ),
            )
        )

    async def verify(invocation: Invocation, view: BoardView) -> tuple[VerifiedObservation, ...]:
        fact = next(
            item for item in view.objects if isinstance(item, Fact) and item.claim_key == "operand"
        )
        return (
            observation(
                view,
                key="answer",
                value=fact.value * 2,
                claim_type="derived",
                topic="result",
                premises=(fact.ref,),
                proof_extra={"operation": "double", "operand": fact.value},
            ),
        )

    registry.register(
        ObservationKnowledgeSource(
            spec(
                "observe",
                kind="observation",
                emitted=("fact",),
                topics=("input",),
                rule=ActivationRule(
                    absent_facts=(FactQuery(claim_key="operand"),), reason="缺少输入观察"
                ),
            ),
            observe,
        )
    )
    registry.register(
        RuleKnowledgeSource(
            spec(
                "explore",
                topics=("input", "strategy"),
                rule=ActivationRule(
                    required_facts=(FactQuery(claim_key="operand"),),
                    absent_hint_topics=("strategy",),
                    reason="已有输入，需要选择求证方向",
                ),
            ),
            explore,
        )
    )
    registry.register(
        VerificationKnowledgeSource(
            spec(
                "verify",
                kind="verification",
                emitted=("fact",),
                topics=("input", "strategy"),
                rule=ActivationRule(
                    required_facts=(FactQuery(claim_key="operand"),),
                    hint_topics=("strategy",),
                    reason="已有输入及求证提示",
                ),
            ),
            verify,
        )
    )
    return registry


async def seed(
    kernel: BlackboardKernel, *, operand: bool = False, hint: bool = False, answer: bool = False
) -> None:
    snapshot = await kernel.store.snapshot("board")
    root = snapshot.by_id()[snapshot.root_intent_id]
    view = BoardView(
        board_id="board",
        board_revision=snapshot.revision,
        root_intent=root,
        intent=root,
        objects=snapshot.objects,
    )
    proposals = []
    for key, value, claim_type in ([("operand", 21, "tool_observation")] if operand else []) + (
        [("answer", 42, "derived")] if answer else []
    ):
        item = observation(view, key=key, value=value, claim_type=claim_type)
        receipt = await kernel.verification.attest(
            "board", item.content, verifier_id=item.verifier_id, evidence=item.proof
        )
        proposals.append(
            FactProposal(
                **item.content.model_dump(),
                local_id=key,
                verification_receipt_id=receipt.receipt_id,
            )
        )
    if hint:
        proposals.append(
            HintProposal(
                local_id="strategy",
                topic="strategy",
                suggestion="尝试把 operand 加倍。",
                applies_when="operand 已确认",
                rationale="先验证这个方向。",
                links=(Link(relation="contributes_to", target=root.ref),),
            )
        )
    if proposals:
        await kernel.commit(
            "board",
            BlackboardDelta(
                expected_revision=snapshot.revision,
                idempotency_key="seed",
                proposals=tuple(proposals),
            ),
            context=CommitContext(source_id="seed"),
        )


async def run_controller(controller: BlackboardController) -> tuple[Any, Any]:
    graph = build_blackboard_graph(controller, checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"board_id": "board"},
        config={
            "configurable": {"thread_id": "board"},
            "recursion_limit": 4 * controller.limits.max_activations + 20,
        },
    )
    return result, graph


@pytest.mark.parametrize(
    "initial,expected",
    [
        ({}, ["observe", "explore", "verify"]),
        ({"operand": True}, ["explore", "verify"]),
        ({"operand": True, "hint": True}, ["verify"]),
        ({"answer": True}, []),
    ],
)
async def test_same_goal_different_blackboards_produce_different_source_paths(initial, expected):
    kernel = await setup_board()
    await seed(kernel, **initial)
    controller = BlackboardController(kernel, pipeline())
    result, graph = await run_controller(controller)
    records = await controller.ledger.records("board")
    assert result["status"] == "succeeded"
    assert [record.candidate.source_id for record in records] == expected
    assert not {"plan", "messages", "assignment", "objects", "output"}.intersection(result)
    assert set(dict(graph.get_subgraphs())) == {"observe", "explore", "verify"}


async def test_unrelated_board_revision_does_not_reactivate_a_source():
    kernel = await setup_board()
    await seed(kernel, operand=True)
    registry = pipeline()
    before = await kernel.store.snapshot("board")
    old = registry.discover(
        before, BoardChangeSet(from_revision=0, to_revision=before.revision), kernel.clock()
    )
    root = before.by_id()[before.root_intent_id]
    await kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=before.revision,
            idempotency_key="unrelated",
            proposals=(
                HintProposal(
                    local_id="unrelated",
                    topic="unrelated",
                    suggestion="另一个主题的经验",
                    applies_when="其他目标",
                    rationale="仅作参考",
                    links=(Link(relation="contributes_to", target=root.ref),),
                ),
            ),
        ),
        context=CommitContext(source_id="other"),
    )
    after = await kernel.store.snapshot("board")
    new = registry.discover(
        after,
        BoardChangeSet(from_revision=before.revision, to_revision=after.revision),
        kernel.clock(),
    )
    assert [item.id for item, _ in old] == [item.id for item, _ in new]


async def test_new_intent_opens_a_branch_without_a_precomputed_task_plan():
    kernel = await setup_board()
    registry = KnowledgeSourceRegistry()

    async def open_direction(invocation, view):
        return KnowledgeOutput(
            proposals=(
                IntentProposal(
                    local_id="obtain-input",
                    topic="observation",
                    objective="取得 operand 的观察值",
                    root_intent_ref=view.root_intent.ref,
                    parent_intent_ref=view.intent.ref,
                    links=(Link(relation="contributes_to", target=view.intent.ref),),
                    acceptance=(
                        AcceptanceCriterion(
                            id="input",
                            description="确认输入值",
                            claim_key="operand",
                            claim_type="tool_observation",
                            operator="equals",
                            expected=21,
                        ),
                    ),
                ),
            )
        )

    async def read_direction(invocation, view):
        return (observation(view, key="operand", value=21),)

    async def verify(invocation, view):
        fact = next(item for item in view.objects if isinstance(item, Fact))
        return (
            observation(
                view,
                key="answer",
                value=fact.value * 2,
                claim_type="derived",
                premises=(fact.ref,),
                proof_extra={"operation": "double", "operand": fact.value},
            ),
        )

    registry.register(
        RuleKnowledgeSource(
            spec(
                "open-direction",
                emitted=("intent",),
                topics=("input",),
                rule=ActivationRule(
                    intent_topics=("result",),
                    absent_facts=(FactQuery(claim_key="operand"),),
                    reason="发现缺少输入，提出局部方向",
                ),
            ),
            open_direction,
        )
    )
    registry.register(
        ObservationKnowledgeSource(
            spec(
                "read-direction",
                kind="observation",
                emitted=("fact",),
                topics=("input",),
                rule=ActivationRule(intent_topics=("observation",), reason="处理新增的观察方向"),
            ),
            read_direction,
        )
    )
    registry.register(
        VerificationKnowledgeSource(
            spec(
                "verify",
                kind="verification",
                emitted=("fact",),
                topics=("input",),
                rule=ActivationRule(
                    intent_topics=("result",),
                    required_facts=(FactQuery(claim_key="operand"),),
                    reason="新分支留下了可用事实",
                ),
            ),
            verify,
        )
    )
    controller = BlackboardController(kernel, registry)
    result, _ = await run_controller(controller)
    assert result["status"] == "succeeded"
    assert [record.candidate.source_id for record in await controller.ledger.records("board")] == [
        "open-direction",
        "read-direction",
        "verify",
    ]
    intents = [
        item for item in (await kernel.store.snapshot("board")).objects if isinstance(item, Intent)
    ]
    assert len(intents) == 2 and all(item.status == "satisfied" for item in intents)


async def test_duplicate_hints_are_not_new_progress_and_do_not_loop_forever():
    kernel = await setup_board()
    registry = KnowledgeSourceRegistry()

    async def repeat(invocation: Invocation, view: BoardView) -> KnowledgeOutput:
        return KnowledgeOutput(
            proposals=(
                HintProposal(
                    local_id="repeat",
                    topic="result",
                    suggestion="同一条提示",
                    applies_when="尚未完成",
                    rationale="尝试验证",
                    derived_from=invocation.record.candidate.input_refs,
                    links=(Link(relation="contributes_to", target=view.intent.ref),),
                ),
            )
        )

    registry.register(RuleKnowledgeSource(spec("repeat"), repeat))
    controller = BlackboardController(kernel, registry)
    result, _ = await run_controller(controller)
    assert result["status"] == "failed"
    records = await controller.ledger.records("board")
    assert [item.status for item in records] == ["completed", "no_progress"]
    assert len((await kernel.store.snapshot("board")).objects) == 2


async def test_rejected_proposal_is_still_charged_and_call_replay_is_not_double_charged():
    kernel = await setup_board()
    registry = KnowledgeSourceRegistry()

    async def invalid(invocation: Invocation, view: BoardView) -> KnowledgeOutput:
        usage = UsageRecord(call_id="call-1", input_tokens=10, output_tokens=20, cost_usd=0.01)
        await invocation.record_usage(usage)
        await invocation.record_usage(usage)
        item = observation(view, key="answer", value=42, claim_type="derived")
        return KnowledgeOutput(
            proposals=(
                FactProposal(
                    **item.content.model_dump(), local_id="bad", verification_receipt_id="invented"
                ),
            )
        )

    registry.register(
        RuleKnowledgeSource(
            spec(
                "invalid",
                emitted=("fact",),
                limits=SourceLimits(token_budget=100, cost_budget_usd=0.1),
            ),
            invalid,
        )
    )
    controller = BlackboardController(kernel, registry)
    result, _ = await run_controller(controller)
    summary = await controller.ledger.summary("board")
    assert summary.tokens == 30 and summary.cost_usd == 0.01
    assert result["status"] == "failed"
    assert (await controller.ledger.records("board"))[0].status == "rejected"
    assert len((await kernel.store.snapshot("board")).objects) == 1


async def test_retry_keeps_activation_identity_and_accounts_for_each_real_attempt():
    kernel = await setup_board()
    registry = KnowledgeSourceRegistry()

    async def retry(invocation: Invocation, view: BoardView) -> KnowledgeOutput:
        await invocation.record_usage(UsageRecord(call_id="call", input_tokens=10))
        if invocation.record.attempt == 1:
            return KnowledgeOutput(status="retryable_error", reason="temporary failure")
        item = observation(view, key="answer", value=42, claim_type="derived")
        proof = await kernel.verification.attest(
            "board", item.content, verifier_id="observed", evidence=item.proof
        )
        return KnowledgeOutput(
            proposals=(
                FactProposal(
                    **item.content.model_dump(),
                    local_id="answer",
                    verification_receipt_id=proof.receipt_id,
                ),
            )
        )

    registry.register(
        RuleKnowledgeSource(
            spec(
                "retry",
                emitted=("fact",),
                limits=SourceLimits(token_budget=100, max_attempts=2),
            ),
            retry,
        )
    )
    controller = BlackboardController(kernel, registry, limits=RunLimits(retry_delay_seconds=0))
    result, _ = await run_controller(controller)
    records = await controller.ledger.records("board")
    assert result["status"] == "succeeded" and len(records) == 1 and records[0].attempt == 2
    summary = await controller.ledger.summary("board")
    assert summary.attempts == 2 and summary.tokens == 20


async def test_historical_token_allowance_does_not_prevent_dispatch():
    kernel = await setup_board()
    registry = KnowledgeSourceRegistry()

    async def forbidden(invocation, view):
        await invocation.record_usage(UsageRecord(call_id="large", input_tokens=60000))
        return KnowledgeOutput()

    registry.register(
        RuleKnowledgeSource(
            spec(
                "expensive",
                limits=SourceLimits(token_budget=100),
            ),
            forbidden,
        )
    )
    controller = BlackboardController(kernel, registry, limits=RunLimits(token_budget=99))
    result, _ = await run_controller(controller)
    assert result["stop_reason"] == "blocked"
    summary = await controller.ledger.summary("board")
    assert summary.attempts == 1 and summary.tokens == 60000


async def test_no_candidates_is_failure_or_partial_but_never_success():
    for has_fact in (False, True):
        kernel = await setup_board()
        await seed(kernel, operand=has_fact)
        controller = BlackboardController(kernel, KnowledgeSourceRegistry())
        result, _ = await run_controller(controller)
        assert result["status"] == ("partial" if has_fact else "failed")
        assert result["stop_reason"] == "blocked"
        assert "answer" in result["final_output"]


async def test_explicit_input_gap_waits_without_repeated_model_calls():
    kernel = await setup_board()
    registry = KnowledgeSourceRegistry()

    async def missing(invocation, view):
        return KnowledgeOutput(
            changes=(
                ObjectChange(
                    target=view.intent.ref,
                    action="block_intent",
                    reason="需要用户提供输入文件",
                ),
            )
        )

    registry.register(RuleKnowledgeSource(spec("input", emitted=("intent",)), missing))
    controller = BlackboardController(kernel, registry)
    result, _ = await run_controller(controller)
    assert result["status"] == "waiting_human"
    assert (await controller.ledger.summary("board")).attempts == 1
    assert (await kernel.store.control("board")).finalization is None
    await kernel.store.check_writer(await kernel.writer("board"))


async def test_source_failure_does_not_prevent_another_viable_source():
    kernel = await setup_board()
    registry = pipeline()

    async def broken(invocation, view):
        raise ValueError("broken branch")

    registry.register(RuleKnowledgeSource(spec("a-broken", topics=("unavailable",)), broken))
    controller = BlackboardController(kernel, registry)
    result, _ = await run_controller(controller)
    assert result["status"] == "succeeded"
    assert (await controller.ledger.records("board"))[0].status == "failed"


async def test_timeout_keeps_unknown_usage_and_retry_is_bounded():
    import asyncio

    kernel = await setup_board()
    registry = KnowledgeSourceRegistry()

    async def slow(invocation, view):
        await asyncio.sleep(1)
        return KnowledgeOutput()

    registry.register(
        RuleKnowledgeSource(
            spec(
                "slow",
                limits=SourceLimits(token_budget=100, timeout_seconds=0.01, max_attempts=2),
            ),
            slow,
        )
    )
    controller = BlackboardController(
        kernel, registry, limits=RunLimits(token_budget=150, retry_delay_seconds=0)
    )
    result, _ = await run_controller(controller)
    summary = await controller.ledger.summary("board")
    assert result["stop_reason"] == "blocked"
    assert summary.reserved_tokens == 0 and summary.attempts == 2
    assert summary.unresolved_attempts == 2 and not summary.usage_complete


async def test_mock_worker_source_publishes_private_draft_as_hint_then_verifier_confirms(
    monkeypatch,
):
    kernel = await setup_board(goal_type="delivery")
    operations = []
    original = MockProvider.complete

    async def record(self, request, *, model):
        operations.append(request.metadata["operation"])
        assert "blackboard" in request.metadata["payload"]["task"]["context"]
        return await original(self, request, model=model)

    monkeypatch.setattr(MockProvider, "complete", record)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        services = AgentServices(
            config=runtime.config,
            gateway=runtime.model_gateway,
            prompts=runtime.prompt_registry,
            trace=TraceRecorder(enabled=False),
            tool_gateway=runtime.tool_gateway,
            skills=runtime.skill_registry,
        )
        registry = KnowledgeSourceRegistry()
        registry.register(
            AgentKnowledgeSource(
                spec(
                    "agent",
                    kind="agent",
                    model="worker-fast",
                    limits=SourceLimits(token_budget=1000, cost_budget_usd=0.1),
                    rule=ActivationRule(absent_hint_topics=("result",), reason="需要候选文档"),
                ),
                services,
            )
        )

        async def inspect(invocation, view):
            hint = next(item for item in view.objects if isinstance(item, Hint))
            content = await invocation.execution.board_store.content(
                view.board_id, hint.attachments[0]
            )
            assert view.intent.objective in content.body
            # This verifies a narrow document contract, not the truth of model-generated claims.
            return (
                observation(
                    view,
                    key="answer",
                    value={"document": content.ref.content_digest},
                    topic="result",
                    claim_type="delivery",
                ),
            )

        registry.register(
            VerificationKnowledgeSource(
                spec(
                    "verify",
                    kind="verification",
                    emitted=("fact",),
                    rule=ActivationRule(hint_topics=("result",), reason="核验候选文档的目标字段"),
                ),
                inspect,
            )
        )
        controller = BlackboardController(kernel, registry)
        result, graph = await run_controller(controller)
        assert result["status"] == "succeeded"
        assert operations == ["worker"]
        assert (await controller.ledger.summary("board")).tokens == 192
        snapshot = await graph.aget_state({"configurable": {"thread_id": "board"}})
        assert not {"messages", "assignment", "output", "artifacts"}.intersection(snapshot.values)
        subgraphs = list(graph.get_subgraphs(recurse=True))
        assert any(
            {"prepare", "model", "tools", "publish"}.issubset(subgraph.get_graph().nodes)
            for _, subgraph in subgraphs
        )
        board = await kernel.store.snapshot("board")
        assert any(isinstance(item, Hint) and item.attachments for item in board.objects)


async def test_agent_usage_is_retained_when_model_output_is_invalid(monkeypatch):
    kernel = await setup_board()
    original = MockProvider.complete

    async def invalid(self, request, *, model):
        response = await original(self, request, model=model)
        return response.model_copy(update={"structured_output": {"unexpected": True}})

    monkeypatch.setattr(MockProvider, "complete", invalid)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        registry = KnowledgeSourceRegistry()
        registry.register(
            AgentKnowledgeSource(
                spec(
                    "agent",
                    kind="agent",
                    model="worker-fast",
                    limits=SourceLimits(token_budget=1000),
                ),
                AgentServices(
                    config=runtime.config,
                    gateway=runtime.model_gateway,
                    prompts=runtime.prompt_registry,
                    trace=TraceRecorder(enabled=False),
                    tool_gateway=runtime.tool_gateway,
                    skills=runtime.skill_registry,
                ),
            )
        )
        controller = BlackboardController(kernel, registry)
        result, _ = await run_controller(controller)
        assert result["status"] == "failed"
        assert (await controller.ledger.summary("board")).tokens == 192


async def test_run_deadline_bounds_a_source_with_a_longer_timeout():
    import asyncio
    import time

    kernel = await setup_board()
    registry = KnowledgeSourceRegistry()

    async def slow(invocation, view):
        await asyncio.sleep(5)
        return KnowledgeOutput()

    registry.register(
        RuleKnowledgeSource(spec("slow", limits=SourceLimits(timeout_seconds=10)), slow)
    )
    controller = BlackboardController(kernel, registry, limits=RunLimits(timeout_seconds=0.05))
    started = time.monotonic()
    result, _ = await run_controller(controller)
    assert result["stop_reason"] == "timeout"
    assert time.monotonic() - started < 1


@pytest.mark.parametrize(
    "limits,expected_attempts,reason",
    [
        (RunLimits(max_activations=3), 3, "activation_limit"),
        (RunLimits(max_objects=2), 2, "blocked"),
    ],
)
async def test_inventing_new_hints_remains_bounded(limits, expected_attempts, reason):
    kernel = await setup_board()
    registry = KnowledgeSourceRegistry()

    async def spam(invocation, view):
        return KnowledgeOutput(
            proposals=(
                HintProposal(
                    local_id="new",
                    topic="result",
                    suggestion=f"新建议 {view.board_revision}",
                    applies_when="目标未完成",
                    rationale="尚需核验",
                    links=(Link(relation="contributes_to", target=view.intent.ref),),
                ),
            )
        )

    registry.register(RuleKnowledgeSource(spec("spam"), spam))
    controller = BlackboardController(kernel, registry, limits=limits)
    result, _ = await run_controller(controller)
    assert result["stop_reason"] == reason
    assert (await controller.ledger.summary("board")).attempts == expected_attempts
    assert len((await kernel.store.snapshot("board")).objects) <= limits.max_objects


async def test_stronger_verification_of_same_content_is_not_deduplicated():
    kernel = await setup_board(goal_type="delivery")
    snapshot = await kernel.store.snapshot("board")
    root = snapshot.by_id()[snapshot.root_intent_id]
    view = BoardView(
        board_id="board",
        board_revision=snapshot.revision,
        root_intent=root,
        intent=root,
        objects=snapshot.objects,
    )
    item = observation(view, key="answer", value="document", claim_type="delivery")
    kernel.verification.register(
        Verifier("weak", "1", "presence-only", ("delivery",), lambda *_: ())
    )
    weak = await kernel.verification.attest(
        "board", item.content, verifier_id="weak", evidence=item.proof
    )
    await kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=snapshot.revision,
            idempotency_key="weak",
            proposals=(
                FactProposal(
                    **item.content.model_dump(),
                    local_id="weak",
                    verification_receipt_id=weak.receipt_id,
                ),
            ),
        ),
        context=CommitContext(source_id="weak"),
    )
    current = await kernel.store.snapshot("board")
    assert current.by_id()[root.id].status == "open"
    strong = await kernel.verification.attest(
        "board", item.content, verifier_id="observed", evidence=item.proof
    )
    await kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=current.revision,
            idempotency_key="strong",
            proposals=(
                FactProposal(
                    **item.content.model_dump(),
                    local_id="strong",
                    verification_receipt_id=strong.receipt_id,
                ),
            ),
        ),
        context=CommitContext(source_id="strong"),
    )
    assert (await kernel.store.snapshot("board")).by_id()[root.id].status == "satisfied"
