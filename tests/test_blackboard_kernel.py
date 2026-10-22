"""Behavioral contracts for phase 1: no models, tools or workflow runner required."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import (
    AcceptanceCriterion,
    BoardObject,
    Evidence,
    Fact,
    FactContent,
    FactProposal,
    HintProposal,
    Intent,
    IntentContent,
    IntentProposal,
    Link,
    ObjectRef,
    digest,
)
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    BoardSnapshot,
    CommitContext,
    IdempotencyConflictError,
    ObjectChange,
    StaleSnapshotError,
    VerificationError,
)
from work_harness.blackboard.store import BlackboardStore
from work_harness.blackboard.verification import VerificationRegistry, Verifier


def check_observation(content: FactContent, evidence: Any) -> tuple[str, ...]:
    """A deterministic fixture verifier for a trusted, structured tool observation."""
    if not isinstance(evidence, dict):
        raise ValueError("observation must be structured")
    for key in ("statement", "claim_key", "claim_type", "value", "scope"):
        if digest(getattr(content, key)) != digest(evidence[key]):
            raise ValueError(f"observation does not prove {key}")
    if any(item.content_digest != digest(evidence) for item in content.evidence):
        raise ValueError("evidence digest mismatch")
    return tuple(evidence["checks"])


@dataclass
class Rig:
    store: BlackboardStore
    registry: VerificationRegistry
    kernel: BlackboardKernel
    board: BoardSnapshot
    now: list[datetime]

    @property
    def root(self) -> ObjectRef:
        return self.board.by_id()[self.board.root_intent_id].ref

    def hint(self, name: str = "hint", **updates: Any) -> HintProposal:
        data = {
            "local_id": name,
            "topic": "latency",
            "suggestion": "Inspect repeated queries",
            "applies_when": "The request repeats reads",
            "rationale": "Repeated reads add work",
            "links": (Link(relation="contributes_to", target=self.root),),
        }
        return HintProposal.model_validate({**data, **updates})

    async def fact(
        self,
        name: str = "answer",
        *,
        value: Any = 42,
        claim_key: str = "answer",
        claim_type: str = "tool_observation",
        scope: dict[str, str] | None = None,
        checks: tuple[str, ...] = ("checked",),
        **updates: Any,
    ) -> FactProposal:
        proof = {
            "statement": f"Observation {claim_key}: {value!r}",
            "claim_key": claim_key,
            "claim_type": claim_type,
            "value": value,
            "scope": scope or {"commit": "abc"},
            "checks": list(checks),
        }
        content = FactContent(
            topic="latency",
            **{key: val for key, val in proof.items() if key != "checks"},
            evidence=(
                Evidence(
                    source="tool",
                    locator=f"tool:{name}",
                    content_digest=digest(proof),
                    description="Trusted tool observation",
                ),
            ),
            **{**{"links": (Link(relation="contributes_to", target=self.root),)}, **updates},
        )
        receipt = await self.registry.attest(
            self.board.board_id,
            content,
            verifier_id="observation",
            evidence=proof,
        )
        return FactProposal(
            **content.model_dump(), local_id=name, verification_receipt_id=receipt.receipt_id
        )

    def intent(self, name: str = "child", **updates: Any) -> IntentProposal:
        content = {
            "local_id": name,
            "topic": "latency",
            "objective": "Find a verified answer",
            "root_intent_ref": self.root,
            "parent_intent_ref": self.root,
            "acceptance": self.board.by_id()[self.board.root_intent_id].acceptance,
            "links": (Link(relation="contributes_to", target=self.root),),
        }
        return IntentProposal.model_validate({**content, **updates})

    async def submit(
        self,
        *proposals: Any,
        changes: tuple[ObjectChange, ...] = (),
        context: CommitContext | None = None,
    ) -> dict[str, BoardObject]:
        current = await self.store.snapshot(self.board.board_id)
        receipt = await self.kernel.commit(
            self.board.board_id,
            BlackboardDelta(
                expected_revision=current.revision,
                idempotency_key=uuid4().hex,
                proposals=proposals,
                changes=changes,
            ),
            context=context or CommitContext(source_id="source"),
        )
        self.board = await self.store.snapshot(self.board.board_id)
        return {key: self.board.by_id()[ref.object_id] for key, ref in receipt.local_refs.items()}


@pytest.fixture
async def rig(blackboard_store: BlackboardStore) -> Rig:
    store = blackboard_store
    registry = VerificationRegistry()
    registry.register(
        Verifier(
            id="observation",
            version="1",
            method="structured-observation",
            claim_types=("tool_observation", "user_statement", "derived", "delivery"),
            check=check_observation,
        )
    )
    now = [datetime(2026, 10, 21, tzinfo=UTC)]
    kernel = BlackboardKernel(store, registry, clock=lambda: now[0])
    board = await kernel.create(
        "run-test",
        IntentContent(
            topic="latency",
            objective="Find the verified answer",
            acceptance=(
                AcceptanceCriterion(
                    id="answer",
                    description="The answer is 42",
                    claim_key="answer",
                    claim_type="tool_observation",
                    operator="equals",
                    expected=42,
                    scope={"commit": "abc"},
                    required_checks=("checked",),
                ),
            ),
        ),
        source_id="user",
    )
    return Rig(store, registry, kernel, board, now)


def local(name: str) -> ObjectRef:
    return ObjectRef(object_id=f"local:{name}", revision=1)


def root_status(rig: Rig) -> str:
    return rig.board.by_id()[rig.board.root_intent_id].status


def test_only_three_board_object_types() -> None:
    schema = TypeAdapter(BoardObject).json_schema()
    assert set(schema["discriminator"]["mapping"]) == {"fact", "intent", "hint"}
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        TypeAdapter(BoardObject).validate_python({"kind": "review"})
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        BlackboardDelta(expected_revision=1, idempotency_key="bad", proposals=[{"kind": "task"}])


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "forged"),
        ("revision", 100),
        ("created_by", "user"),
        ("status", "satisfied"),
    ],
)
def test_proposal_cannot_assign_kernel_fields(rig: Rig, field: str, value: Any) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        HintProposal.model_validate({**rig.hint().model_dump(), field: value})


async def test_unverified_fact_rolls_back_entire_batch(rig: Rig) -> None:
    fact = (await rig.fact()).model_copy(update={"verification_receipt_id": "model-says-confirmed"})
    with pytest.raises(VerificationError, match="unknown verification"):
        await rig.submit(rig.hint(), fact)
    assert await rig.store.snapshot(rig.board.board_id) == rig.board
    assert len(await rig.store.events(rig.board.board_id)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"statement": "Another claim"},
        {"value": 43},
        {"scope": {"commit": "other"}},
        {"claim_type": "delivery"},
        {"claim_key": "another-answer"},
    ],
)
async def test_receipt_is_bound_to_claim_and_scope(rig: Rig, change: dict[str, Any]) -> None:
    with pytest.raises(VerificationError, match="does not cover"):
        await rig.submit((await rig.fact()).model_copy(update=change))


async def test_verifier_rejects_evidence_that_does_not_support_claim(rig: Rig) -> None:
    proposal = await rig.fact()
    content = FactContent.model_validate(proposal.model_dump(include=set(FactContent.model_fields)))
    with pytest.raises(VerificationError, match="does not prove"):
        await rig.registry.attest(
            rig.board.board_id,
            content,
            verifier_id="observation",
            evidence={"statement": "different"},
        )


async def test_user_statement_cannot_satisfy_external_observation(rig: Rig) -> None:
    await rig.submit(await rig.fact(claim_type="user_statement"))
    assert root_status(rig) == "open"
    limited = VerificationRegistry()
    limited.register(
        Verifier("user-input", "1", "user-input", ("user_statement",), check_observation)
    )
    proposal = await rig.fact()
    content = FactContent.model_validate(proposal.model_dump(include=set(FactContent.model_fields)))
    with pytest.raises(VerificationError, match="not authorized"):
        await limited.attest(rig.board.board_id, content, verifier_id="user-input", evidence={})


async def test_success_requires_matching_scope_value_and_checks(rig: Rig) -> None:
    await rig.submit(rig.hint())
    assert root_status(rig) == "open"
    await rig.submit(await rig.fact("elsewhere", scope={"commit": "elsewhere"}))
    assert root_status(rig) == "open"
    await rig.submit(await rig.fact("unchecked", checks=()))
    assert root_status(rig) == "open"
    accepted = await rig.submit(await rig.fact("checked"))
    root = rig.board.by_id()[rig.board.root_intent_id]
    assert root.status == "satisfied"
    assert root.satisfied_by == (accepted["checked"].ref,)


async def test_same_commit_refs_support_forward_causal_links(rig: Rig) -> None:
    fact = await rig.fact(links=(Link(relation="contributes_to", target=local("child")),))
    child = rig.intent()
    hint = rig.hint(derived_from=(local("answer"),))
    created = await rig.submit(hint, fact, child)
    assert created["hint"].derived_from == (created["answer"].ref,)
    assert created["answer"].links[0].target == created["child"].ref
    assert created["child"].status == "satisfied"
    assert root_status(rig) == "satisfied"


async def test_json_value_shaped_like_reference_is_not_rewritten(rig: Rig) -> None:
    value = {"object_id": "local:hint", "revision": 1}
    created = await rig.submit(await rig.fact(value=value), rig.hint())
    assert created["answer"].value == value


@pytest.mark.parametrize("relation", ["derived_from", "parent_intent_ref"])
async def test_causal_and_parent_cycles_are_rejected(rig: Rig, relation: str) -> None:
    if relation == "derived_from":
        proposals = (
            rig.hint("a", derived_from=(local("b"),)),
            rig.hint("b", derived_from=(local("a"),)),
        )
    else:
        proposals = (
            rig.intent("a", parent_intent_ref=local("b")),
            rig.intent("b", parent_intent_ref=local("a")),
        )
    with pytest.raises(BlackboardError, match="cycle"):
        await rig.submit(*proposals)
    assert len((await rig.store.snapshot(rig.board.board_id)).objects) == 1


async def test_semantic_links_may_form_cycles(rig: Rig) -> None:
    created = await rig.submit(
        rig.hint(
            "a",
            links=(
                Link(relation="contributes_to", target=rig.root),
                Link(relation="supports", target=local("b")),
            ),
        ),
        rig.hint(
            "b",
            links=(
                Link(relation="contributes_to", target=rig.root),
                Link(relation="supports", target=local("a")),
            ),
        ),
    )
    assert set(created) == {"a", "b"}


async def test_cross_board_receipts_and_references_are_rejected(rig: Rig) -> None:
    other = await rig.kernel.create(
        "another",
        IntentContent(
            topic="other",
            objective="Other goal",
            acceptance=rig.board.objects[0].acceptance,
        ),
        source_id="user",
    )
    with pytest.raises(BlackboardError, match="missing from this board"):
        await rig.submit(rig.hint(derived_from=(other.objects[0].ref,)))
    proposal = await rig.fact()
    with pytest.raises(VerificationError, match="unknown verification"):
        await rig.kernel.commit(
            "another",
            BlackboardDelta(
                expected_revision=other.revision,
                idempotency_key="cross-board",
                proposals=(proposal,),
            ),
            context=CommitContext(source_id="source"),
        )


async def test_conflicting_facts_reopen_goal_and_create_one_resolution_intent(rig: Rig) -> None:
    first = (await rig.submit(await rig.fact("first")))["first"]
    old_board = rig.board
    assert root_status(rig) == "satisfied"
    await rig.submit(await rig.fact("second", value=43))
    assert root_status(rig) == "open"
    facts = [item for item in rig.board.objects if isinstance(item, Fact)]
    assert {item.lifecycle for item in facts} == {"contested"}
    conflicts = [item for item in rig.board.objects if isinstance(item, Intent) and item.blockers]
    assert len(conflicts) == 1
    assert await rig.store.get_object(rig.board.board_id, first.ref) == first
    assert old_board.by_id()[old_board.root_intent_id].status == "satisfied"
    await rig.submit()
    assert (
        len([item for item in rig.board.objects if isinstance(item, Intent) and item.blockers]) == 1
    )


async def test_conflict_resolution_requires_authorized_verified_replacement(rig: Rig) -> None:
    await rig.submit(await rig.fact("a"), await rig.fact("b", value=43))
    conflicts = [item for item in rig.board.objects if isinstance(item, Fact)]
    replacement = await rig.fact(
        "resolved",
        links=(
            Link(relation="contributes_to", target=rig.root),
            *(Link(relation="supersedes", target=item.ref) for item in conflicts),
        ),
    )
    with pytest.raises(BlackboardError, match="authorized"):
        await rig.submit(replacement)
    await rig.submit(
        replacement, context=CommitContext(source_id="verifier", can_supersede_facts=True)
    )
    assert root_status(rig) == "satisfied"
    assert all(item.status == "satisfied" for item in rig.board.objects if isinstance(item, Intent))
    assert {rig.board.by_id()[item.id].lifecycle for item in conflicts} == {"superseded"}


async def test_consumed_hint_does_not_invalidate_independently_proven_fact(rig: Rig) -> None:
    hint = (await rig.submit(rig.hint()))["hint"]
    fact = (await rig.submit(await rig.fact(derived_from=(hint.ref,))))["answer"]
    await rig.submit(
        changes=(ObjectChange(target=hint.ref, action="consume_hint", reason="Applied"),)
    )
    assert rig.board.by_id()[fact.id] == fact
    assert root_status(rig) == "satisfied"


async def test_retracted_premise_invalidates_proof_chain_but_not_independent_fact(rig: Rig) -> None:
    first = (await rig.submit(await rig.fact("a", claim_key="source")))["a"]
    second = (
        await rig.submit(await rig.fact("b", claim_key="derived", premise_refs=(first.ref,)))
    )["b"]
    created = await rig.submit(
        await rig.fact("answer", premise_refs=(second.ref,)),
        await rig.fact("unrelated", claim_key="unrelated"),
    )
    assert root_status(rig) == "satisfied"
    await rig.submit(
        changes=(ObjectChange(target=first.ref, action="retract_fact", reason="Bad input"),),
        context=CommitContext(source_id="verifier", can_retract_facts=True),
    )
    current = rig.board.by_id()
    assert current[second.id].lifecycle == "stale"
    assert current[created["answer"].id].lifecycle == "stale"
    assert current[created["unrelated"].id] == created["unrelated"]
    assert root_status(rig) == "open"


async def test_hint_cannot_be_a_fact_premise(rig: Rig) -> None:
    hint = (await rig.submit(rig.hint()))["hint"]
    with pytest.raises(BlackboardError, match="current valid Fact"):
        await rig.submit(await rig.fact(premise_refs=(hint.ref,)))


async def test_expired_fact_reopens_goal(rig: Rig) -> None:
    fact = (await rig.submit(await rig.fact(valid_until=rig.now[0] + timedelta(hours=1))))["answer"]
    assert root_status(rig) == "satisfied"
    rig.now[0] += timedelta(hours=2)
    await rig.submit()
    assert rig.board.by_id()[fact.id].lifecycle == "expired"
    assert root_status(rig) == "open"


async def test_idempotent_retry_returns_original_receipt_and_rejects_changed_body(rig: Rig) -> None:
    delta = BlackboardDelta(expected_revision=1, idempotency_key="once", proposals=(rig.hint(),))
    context = CommitContext(source_id="source")
    receipts = await asyncio.gather(
        *[rig.kernel.commit(rig.board.board_id, delta, context=context) for _ in range(4)]
    )
    assert all(item == receipts[0] for item in receipts)
    assert len(await rig.store.events(rig.board.board_id)) == 2
    with pytest.raises(IdempotencyConflictError):
        await rig.kernel.commit(
            rig.board.board_id, delta.model_copy(update={"proposals": ()}), context=context
        )


async def test_competing_snapshot_commits_cannot_overwrite_each_other(rig: Rig) -> None:
    # Both requests are prepared against revision 1 before either enters publication.
    original_commit = rig.store.commit
    barrier = asyncio.Barrier(2)

    async def concurrent_commit(request, *, writer):
        await barrier.wait()
        return await original_commit(request, writer=writer)

    rig.store.commit = concurrent_commit
    results = await asyncio.gather(
        *[
            rig.kernel.commit(
                rig.board.board_id,
                BlackboardDelta(
                    expected_revision=1,
                    idempotency_key=key,
                    proposals=(rig.hint(key),),
                ),
                context=CommitContext(source_id="source"),
            )
            for key in ("one", "two")
        ],
        return_exceptions=True,
    )
    rig.store.commit = original_commit
    assert sum(isinstance(item, StaleSnapshotError) for item in results) == 1
    assert len((await rig.store.snapshot(rig.board.board_id)).objects) == 2


async def test_root_cannot_be_reactivated_after_user_abandons_it(rig: Rig) -> None:
    await rig.submit(
        changes=(ObjectChange(target=rig.root, action="abandon_intent", reason="User cancelled"),),
        context=CommitContext(source_id="user", is_user=True),
    )
    with pytest.raises(BlackboardError, match="only user"):
        await rig.submit(
            changes=(
                ObjectChange(
                    target=rig.root,
                    action="reopen_intent",
                    reason="Agent wants to continue",
                ),
            )
        )
    events = await rig.store.events(rig.board.board_id)
    assert events[-1].requested_changes[0].reason == "User cancelled"


async def test_read_set_rejects_historical_fact_version(rig: Rig) -> None:
    first = (await rig.submit(await rig.fact("first")))["first"]
    await rig.submit(await rig.fact("second", value=43))
    with pytest.raises(StaleSnapshotError, match="read set"):
        await rig.kernel.commit(
            rig.board.board_id,
            BlackboardDelta(
                expected_revision=rig.board.revision,
                idempotency_key="old-input",
                read_set=(first.ref,),
                proposals=(rig.hint(),),
            ),
            context=CommitContext(source_id="source"),
        )


async def test_snapshots_and_receipts_do_not_alias_store_state(rig: Rig) -> None:
    await rig.submit(await rig.fact())
    snapshot = await rig.store.snapshot(rig.board.board_id)
    fact = next(item for item in snapshot.objects if isinstance(item, Fact))
    fact.scope["commit"] = "tampered"
    assert (await rig.store.get_object(rig.board.board_id, fact.ref)).scope == {"commit": "abc"}


async def test_knowledge_source_cannot_abandon_root_or_publish_forbidden_kind(rig: Rig) -> None:
    with pytest.raises(BlackboardError, match="only user"):
        await rig.submit(
            changes=(ObjectChange(target=rig.root, action="abandon_intent", reason="Too hard"),)
        )
    with pytest.raises(BlackboardError, match="cannot emit"):
        await rig.submit(
            await rig.fact(), context=CommitContext(source_id="hint-only", allowed_kinds=("hint",))
        )


@pytest.mark.parametrize(
    "bad",
    [
        (),
        (
            AcceptanceCriterion(
                id="x",
                description="test",
                claim_key="x",
                claim_type="tool_observation",
            ),
        )
        * 2,
    ],
)
def test_goal_cannot_have_empty_or_duplicate_criteria(bad: tuple[AcceptanceCriterion, ...]) -> None:
    with pytest.raises(ValidationError):
        IntentContent(topic="test", objective="test", acceptance=bad)


async def test_source_cannot_inflate_child_intent_priority(rig: Rig) -> None:
    created = await rig.submit(rig.intent(priority=100))
    assert created["child"].priority == rig.board.by_id()[rig.board.root_intent_id].priority == 0
