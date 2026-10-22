"""Pure lifecycle reconciliation, separate from storage and execution."""

from __future__ import annotations

from datetime import datetime
from itertools import combinations
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from work_harness.blackboard.goals import evaluate, fact_is_effective
from work_harness.blackboard.models import (
    AcceptanceCriterion,
    BoardObject,
    Fact,
    Hint,
    Intent,
    IntentContent,
    Link,
    ObjectRef,
    content_of,
    digest,
)


def object_id(board_id: str, *parts: str) -> str:
    return f"bb_{uuid5(NAMESPACE_URL, digest([board_id, *parts])).hex}"


def revise(item: BoardObject, original: dict[str, BoardObject], **changes: Any) -> BoardObject:
    data = item.model_dump()
    data.update(changes)
    data["revision"] = original[item.id].revision + 1 if item.id in original else 1
    updated = type(item).model_validate(data)
    return updated.model_copy(update={"content_digest": digest(content_of(updated))})


def scopes_overlap(left: Fact, right: Fact) -> bool:
    if any(right.scope[key] != value for key, value in left.scope.items() if key in right.scope):
        return False
    if left.valid_until and right.valid_from and left.valid_until <= right.valid_from:
        return False
    if right.valid_until and left.valid_from and right.valid_until <= left.valid_from:
        return False
    return True


def reconcile(
    objects: dict[str, BoardObject],
    original: dict[str, BoardObject],
    *,
    board_id: str,
    root_intent_id: str,
    now: datetime,
) -> None:
    for item in list(objects.values()):
        if isinstance(item, Fact) and item.lifecycle in {"valid", "contested", "stale"}:
            if item.valid_until and item.valid_until <= now:
                objects[item.id] = revise(item, original, lifecycle="expired")
        if isinstance(item, Hint) and item.status == "active":
            if item.expires_at and item.expires_at <= now:
                objects[item.id] = revise(item, original, status="expired", status_reason="expired")

    facts = [
        item
        for item in objects.values()
        if isinstance(item, Fact)
        and item.lifecycle in {"valid", "contested"}
        and (item.valid_from is None or item.valid_from <= now)
    ]
    for left, right in combinations(facts, 2):
        if (
            not left.claim_key
            or left.claim_key != right.claim_key
            or left.claim_type != right.claim_type
            or digest(left.value) == digest(right.value)
            or not scopes_overlap(left, right)
        ):
            continue
        for item in (left, right):
            if objects[item.id].lifecycle != "contested":
                objects[item.id] = revise(objects[item.id], original, lifecycle="contested")
        conflict_id = object_id(board_id, "conflict", *sorted((left.id, right.id)))
        if conflict_id in objects:
            continue
        root = objects[root_intent_id]
        content = IntentContent(
            topic=left.topic,
            objective=f"Resolve conflicting evidence for {left.claim_key}",
            root_intent_ref=root.ref,
            parent_intent_ref=root.ref,
            scope={**left.scope, **right.scope},
            priority=100,
            derived_from=(objects[left.id].ref, objects[right.id].ref),
            links=(Link(relation="contributes_to", target=root.ref),),
            acceptance=(
                AcceptanceCriterion(
                    id="resolved",
                    description="An uncontested verified replacement is available",
                    claim_key=left.claim_key,
                    claim_type=left.claim_type,
                    scope={**left.scope, **right.scope},
                ),
            ),
        )
        objects[conflict_id] = Intent(
            **content.model_dump(),
            id=conflict_id,
            board_id=board_id,
            revision=1,
            created_at=now,
            created_by="blackboard.kernel",
            content_digest=digest(content),
            blockers=(objects[left.id].ref, objects[right.id].ref),
        )

    # Only proof dependencies propagate invalidity; inspiration and ordinary links do not.
    changed = True
    while changed:
        changed = False
        for item in list(objects.values()):
            if isinstance(item, Fact) and item.lifecycle == "valid" and item.premise_refs:
                if any(
                    not isinstance(premise := objects.get(ref.object_id), Fact)
                    or premise.ref != ref
                    or not fact_is_effective(premise, objects, now)
                    for ref in item.premise_refs
                ):
                    objects[item.id] = revise(item, original, lifecycle="stale")
                    changed = True

    for item in list(objects.values()):
        if not isinstance(item, Intent) or item.status == "abandoned":
            continue
        result = evaluate(item, objects, now)
        status = (
            "satisfied" if result.satisfied else ("blocked" if item.status == "blocked" else "open")
        )
        witnesses = result.witnesses if result.satisfied else ()
        if status != item.status or witnesses != item.satisfied_by:
            objects[item.id] = revise(item, original, status=status, satisfied_by=witnesses)


def referenced_objects(item: BoardObject) -> tuple[ObjectRef, ...]:
    refs = list(item.derived_from) + [link.target for link in item.links]
    if isinstance(item, Fact):
        refs.extend(item.premise_refs)
    if isinstance(item, Intent):
        refs.extend(ref for ref in (item.root_intent_ref, item.parent_intent_ref) if ref)
    if isinstance(item, Hint):
        refs.extend(item.source_refs)
    return tuple(refs)
