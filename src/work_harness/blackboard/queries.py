"""Finite read conditions, evaluated against the committed snapshot inside publication."""

from __future__ import annotations

from datetime import datetime

from work_harness.blackboard.goals import contributes_to, evaluate, fact_is_effective
from work_harness.blackboard.models import BoardObject, Fact, Hint, Intent
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BoardSnapshot,
    FactQuery,
    FactQueryPrecondition,
    HintQueryPrecondition,
    IntentPrecondition,
    ReadSelector,
    StaleSnapshotError,
    ViewPrecondition,
)


def query_matches(query: FactQuery, fact: Fact) -> bool:
    return (
        fact.claim_key == query.claim_key
        and (query.claim_type is None or fact.claim_type == query.claim_type)
        and all(fact.scope.get(key) == value for key, value in query.scope.items())
    )


def select_objects(
    snapshot: BoardSnapshot, selector: ReadSelector, now: datetime
) -> tuple[BoardObject, ...]:
    objects = snapshot.by_id()
    intent = objects.get(selector.intent_id)
    if not isinstance(intent, Intent):
        raise StaleSnapshotError("query target is not an Intent on this board")
    selected = {intent.id: intent}
    for item in snapshot.objects:
        if item.kind not in selector.accepted_kinds:
            continue
        if selector.input_topics and item.topic not in selector.input_topics:
            continue
        if not contributes_to(item, intent.id, objects) and not any(
            link.relation == "contributes_to" and link.target.object_id == snapshot.root_intent_id
            for link in item.links
        ):
            continue
        if isinstance(item, Fact):
            contested = (
                item.lifecycle == "contested"
                and (item.valid_from is None or item.valid_from <= now)
                and (item.valid_until is None or item.valid_until > now)
            )
            if contested or fact_is_effective(item, objects, now):
                selected[item.id] = item
        elif isinstance(item, Hint):
            if item.status == "active" and (item.expires_at is None or item.expires_at > now):
                selected[item.id] = item
    # Proof premises are readable even outside the topic filter or when being disputed.
    pending = list(selected.values())
    while pending:
        item = pending.pop()
        if isinstance(item, Fact):
            for ref in item.verification.premise_refs:
                if ref.object_id in objects and ref.object_id not in selected:
                    selected[ref.object_id] = objects[ref.object_id]
                    pending.append(objects[ref.object_id])
    return tuple(sorted(selected.values(), key=lambda item: item.id))


def check_preconditions(snapshot: BoardSnapshot, delta: BlackboardDelta, now: datetime) -> None:
    if not 1 <= delta.expected_revision <= snapshot.revision:
        raise StaleSnapshotError("read snapshot revision is not available")
    if delta.concurrency == "snapshot" and snapshot.revision != delta.expected_revision:
        raise StaleSnapshotError("board revision no longer matches the read snapshot")
    objects = snapshot.by_id()
    for ref in delta.read_set:
        if ref.object_id not in objects or objects[ref.object_id].ref != ref:
            raise StaleSnapshotError("read set contains an outdated object")
    views: dict[str, tuple[BoardObject, ...]] = {}
    for condition in delta.preconditions:
        if isinstance(condition, IntentPrecondition):
            intent = objects.get(condition.intent_id)
            allowed = {"open", "blocked"} if condition.allow_blocked else {"open"}
            if (
                not isinstance(intent, Intent)
                or intent.status not in allowed
                or evaluate(intent, objects, now).satisfied
            ):
                raise StaleSnapshotError("activation target is no longer open")
            continue
        key = condition.selector.model_dump_json()
        if key not in views:
            views[key] = select_objects(snapshot, condition.selector, now)
        selected = views[key]
        if isinstance(condition, ViewPrecondition):
            if {item.ref for item in selected} != set(condition.expected_refs):
                raise StaleSnapshotError("read query membership changed")
        elif isinstance(condition, FactQueryPrecondition):
            found = any(
                isinstance(item, Fact)
                and query_matches(condition.query, item)
                and fact_is_effective(item, objects, now)
                for item in selected
            )
            if found != condition.exists:
                raise StaleSnapshotError("Fact query precondition no longer holds")
        elif isinstance(condition, HintQueryPrecondition):
            found = any(
                isinstance(item, Hint)
                and item.topic == condition.topic
                and item.status == "active"
                and (item.expires_at is None or item.expires_at > now)
                for item in selected
            )
            if found != condition.exists:
                raise StaleSnapshotError("Hint query precondition no longer holds")
