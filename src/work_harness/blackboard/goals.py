"""Deterministic goal evaluation over current, supported facts."""

from __future__ import annotations

from datetime import datetime

from work_harness.blackboard.models import (
    AcceptanceCriterion,
    BoardModel,
    BoardObject,
    Fact,
    Intent,
    ObjectRef,
    digest,
)


class GoalEvaluation(BoardModel):
    satisfied: bool
    witnesses: tuple[ObjectRef, ...] = ()
    missing_criteria: tuple[str, ...] = ()
    conflicts: tuple[ObjectRef, ...] = ()


def fact_is_effective(
    fact: Fact,
    objects: dict[str, BoardObject],
    now: datetime,
    visiting: frozenset[str] = frozenset(),
) -> bool:
    if fact.id in visiting or fact.lifecycle != "valid":
        return False
    if (fact.valid_from and fact.valid_from > now) or (
        fact.valid_until and fact.valid_until <= now
    ):
        return False
    for ref in fact.verification.premise_refs:
        premise = objects.get(ref.object_id)
        if (
            not isinstance(premise, Fact)
            or premise.revision != ref.revision
            or not fact_is_effective(premise, objects, now, visiting | {fact.id})
        ):
            return False
    return True


def contributes_to(item: BoardObject, intent_id: str, objects: dict[str, BoardObject]) -> bool:
    for link in item.links:
        if link.relation != "contributes_to":
            continue
        target = objects.get(link.target.object_id)
        seen: set[str] = set()
        while isinstance(target, Intent) and target.id not in seen:
            if target.id == intent_id:
                return True
            seen.add(target.id)
            target = (
                objects.get(target.parent_intent_ref.object_id)
                if (target.parent_intent_ref)
                else None
            )
    return False


def matches(criterion: AcceptanceCriterion, fact: Fact) -> bool:
    if fact.claim_key != criterion.claim_key or fact.claim_type != criterion.claim_type:
        return False
    if any(fact.scope.get(key) != value for key, value in criterion.scope.items()):
        return False
    if not set(criterion.required_checks).issubset(fact.verification.checks):
        return False
    if criterion.operator == "exists":
        return True
    if criterion.operator == "equals":
        return digest(fact.value) == digest(criterion.expected)
    if isinstance(fact.value, bool) or not isinstance(fact.value, (int, float)):
        return False
    # The criterion contract restricts numeric comparisons to numeric operands.
    if criterion.operator == "at_least":
        return fact.value >= criterion.expected
    return fact.value <= criterion.expected


def evaluate(intent: Intent, objects: dict[str, BoardObject], now: datetime) -> GoalEvaluation:
    facts = sorted(
        (item for item in objects.values() if isinstance(item, Fact)), key=lambda x: x.id
    )
    relevant = [item for item in facts if contributes_to(item, intent.id, objects)]
    blocker_ids = {ref.object_id for ref in intent.blockers}
    conflicts = tuple(
        item.ref
        for item in facts
        if item.lifecycle == "contested"
        and (item.id in blocker_ids or contributes_to(item, intent.id, objects))
    )
    eligible = [item for item in relevant if fact_is_effective(item, objects, now)]
    # Conflict-resolution Intents may use a verified replacement associated with their parent.
    if blocker_ids:
        eligible.extend(
            item
            for item in facts
            if fact_is_effective(item, objects, now)
            and any(
                link.relation == "supersedes" and link.target.object_id in blocker_ids
                for link in item.links
            )
        )
    witnesses: dict[str, ObjectRef] = {}
    missing: list[str] = []
    for criterion in intent.acceptance:
        witness = next((item for item in eligible if matches(criterion, item)), None)
        if witness:
            witnesses[witness.id] = witness.ref
        else:
            missing.append(criterion.id)
    return GoalEvaluation(
        satisfied=not missing and not conflicts,
        witnesses=tuple(witnesses.values()),
        missing_criteria=tuple(missing),
        conflicts=conflicts,
    )
