"""Deterministic source matching and stable identities over effective inputs."""

from __future__ import annotations

from datetime import UTC, datetime

from work_harness.blackboard.goals import fact_is_effective
from work_harness.blackboard.models import Fact, Hint, Intent, digest
from work_harness.blackboard.projections import object_id
from work_harness.blackboard.protocol import (
    BlackboardError,
    BoardSnapshot,
    FactQueryPrecondition,
    HintQueryPrecondition,
    IntentPrecondition,
    QueryPrecondition,
    ReadSelector,
    ViewPrecondition,
)
from work_harness.blackboard.queries import query_matches, select_objects
from work_harness.knowledge_sources.base import (
    ActivationCandidate,
    BoardView,
    KnowledgeSourceSpec,
)


def make_view(
    snapshot: BoardSnapshot,
    intent: Intent,
    spec: KnowledgeSourceSpec,
    now: datetime,
) -> BoardView:
    objects = snapshot.by_id()
    root = objects[snapshot.root_intent_id]
    if not isinstance(root, Intent):
        raise BlackboardError("board root is not an Intent")
    selected = select_objects(snapshot, selector_for(spec, intent.id), now)
    return BoardView(
        board_id=snapshot.board_id,
        board_revision=snapshot.revision,
        root_intent=root,
        intent=intent,
        objects=selected,
        evaluated_at=now,
    )


def match_rule(spec: KnowledgeSourceSpec, view: BoardView) -> ActivationCandidate | None:
    rule = spec.activation_rule
    if view.intent.status not in ({"open", "blocked"} if rule.accept_blocked else {"open"}):
        return None
    if rule.intent_topics and view.intent.topic not in rule.intent_topics:
        return None
    now = view.evaluated_at or datetime.now(UTC)
    facts = [
        item
        for item in view.objects
        if isinstance(item, Fact) and fact_is_effective(item, view.by_id(), now)
    ]
    hints = [
        item
        for item in view.objects
        if isinstance(item, Hint)
        and item.status == "active"
        and (item.expires_at is None or item.expires_at > now)
    ]
    if any(not any(query_matches(query, fact) for fact in facts) for query in rule.required_facts):
        return None
    if any(any(query_matches(query, fact) for fact in facts) for query in rule.absent_facts):
        return None
    if any(not any(hint.topic == topic for hint in hints) for topic in rule.hint_topics):
        return None
    if any(any(hint.topic == topic for hint in hints) for topic in rule.absent_hint_topics):
        return None
    return candidate_for_view(spec, view)


def candidate_for_view(spec: KnowledgeSourceSpec, view: BoardView) -> ActivationCandidate:
    # The whole supplied view is readable by the source; fingerprint every supplied object.
    # Unrelated board revisions and unrelated branches are deliberately excluded.
    refs = tuple(
        sorted(
            {item.id: item.ref for item in (view.root_intent, *view.objects)}.values(),
            key=lambda ref: ref.object_id,
        )
    )
    fingerprint = digest(
        {
            "inputs": [ref.model_dump(mode="json") for ref in refs],
            "queries": spec.activation_rule.model_dump(mode="json"),
            "root_goal": view.root_intent.content_digest,
        }
    )
    return ActivationCandidate(
        id=object_id(
            view.board_id,
            "activation",
            spec.id,
            spec.version,
            view.intent.id,
            str(view.intent.revision),
            fingerprint,
            spec.strategy_version,
        ),
        board_id=view.board_id,
        source_id=spec.id,
        source_version=spec.version,
        intent_ref=view.intent.ref,
        input_refs=refs,
        input_fingerprint=fingerprint,
        strategy_version=spec.strategy_version,
        reason=spec.activation_rule.reason,
        conflict=bool(view.intent.blockers),
        intent_priority=view.intent.priority,
    )


def selector_for(spec: KnowledgeSourceSpec, intent_id: str) -> ReadSelector:
    return ReadSelector(
        intent_id=intent_id,
        input_topics=spec.input_topics,
        accepted_kinds=spec.accepted_kinds,
    )


def activation_preconditions(
    spec: KnowledgeSourceSpec, view: BoardView
) -> tuple[QueryPrecondition, ...]:
    """Capture host-owned reads; a source output cannot remove its activation prerequisites."""
    selector = selector_for(spec, view.intent.id)
    rule = spec.activation_rule
    conditions: list[QueryPrecondition] = [
        IntentPrecondition(intent_id=view.root_intent.id, allow_blocked=True),
        IntentPrecondition(intent_id=view.intent.id, allow_blocked=rule.accept_blocked),
    ]
    for exists, queries in ((True, rule.required_facts), (False, rule.absent_facts)):
        conditions.extend(
            FactQueryPrecondition(selector=selector, query=query, exists=exists)
            for query in queries
        )
    for exists, topics in ((True, rule.hint_topics), (False, rule.absent_hint_topics)):
        conditions.extend(
            HintQueryPrecondition(selector=selector, topic=topic, exists=exists) for topic in topics
        )
    conditions.append(
        ViewPrecondition(selector=selector, expected_refs=tuple(i.ref for i in view.objects))
    )
    return tuple(conditions)
