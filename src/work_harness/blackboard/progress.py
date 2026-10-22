"""Bounded, deterministic goal progress; knowledge volume is not a progress signal."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field

from work_harness.blackboard.activation import make_view, match_rule
from work_harness.blackboard.goals import contributes_to, evaluate, fact_is_effective
from work_harness.blackboard.models import (
    AcceptanceCriterion,
    BoardModel,
    Fact,
    Hint,
    Intent,
    ObjectRef,
    digest,
)
from work_harness.blackboard.protocol import BoardSnapshot, FactQuery
from work_harness.blackboard.queries import query_matches
from work_harness.knowledge_sources.base import KnowledgeSourceSpec


class ExplorationDirection(BoardModel):
    source_id: str
    intent_ref: ObjectRef
    input_fingerprint: str


class ProgressSnapshot(BoardModel):
    board_revision: int
    evaluated_at: AwareDatetime
    evidence: dict[str, ObjectRef] = Field(default_factory=dict)
    satisfied: tuple[str, ...] = ()
    gaps: dict[str, str] = Field(default_factory=dict)
    conflicts: tuple[str, ...] = ()
    directions: dict[str, ExplorationDirection] = Field(default_factory=dict)


class ProgressDecision(BoardModel):
    kind: Literal["goal_progress", "exploration", "no_progress"]
    before_revision: int
    after_revision: int
    reasons: tuple[str, ...]
    new_evidence: tuple[str, ...] = ()
    closed_gaps: tuple[str, ...] = ()
    resolved_conflicts: tuple[str, ...] = ()
    new_directions: tuple[str, ...] = ()


class ProgressState(BoardModel):
    snapshot: ProgressSnapshot
    exploration_remaining: int = Field(ge=0)
    seen_evidence: tuple[str, ...] = ()
    seen_satisfied: tuple[str, ...] = ()
    seen_resolutions: tuple[str, ...] = ()
    seen_directions: tuple[str, ...] = ()
    last_decision: ProgressDecision | None = None


def criterion_key(criterion: AcceptanceCriterion) -> str:
    return digest(criterion.model_dump(mode="json", exclude={"id", "description"}))


def _intent_key(intent: Intent) -> str:
    return digest(
        {
            "topic": intent.topic,
            "scope": intent.scope,
            "acceptance": sorted({criterion_key(item) for item in intent.acceptance}),
        }
    )


def _related(intent: Intent, board: BoardSnapshot, specs: tuple[KnowledgeSourceSpec, ...]) -> bool:
    objects = board.by_id()
    root = objects[board.root_intent_id]
    if intent.id == root.id:
        return True
    cursor, visited = intent, set()
    while cursor.id != root.id:
        if cursor.id in visited or cursor.parent_intent_ref is None or cursor.status == "abandoned":
            return False
        visited.add(cursor.id)
        cursor = objects.get(cursor.parent_intent_ref.object_id)
        if not isinstance(cursor, Intent):
            return False
    # A parent link alone cannot make an arbitrary new acceptance condition a root obligation.
    requirements = [
        FactQuery(claim_key=c.claim_key, claim_type=c.claim_type, scope=c.scope)
        for c in root.acceptance
    ] + [q for spec in specs for q in spec.activation_rule.required_facts]
    return any(
        c.claim_key == q.claim_key
        and q.claim_type in (None, c.claim_type)
        and all(c.scope.get(key) == value for key, value in q.scope.items())
        for c in intent.acceptance
        for q in requirements
    )


def summarize(
    board: BoardSnapshot,
    specs: tuple[KnowledgeSourceSpec, ...],
    now: datetime,
) -> ProgressSnapshot:
    objects = board.by_id()
    root = objects[board.root_intent_id]
    evaluation = evaluate(root, objects, now)
    evidence: dict[str, ObjectRef] = {}
    conflicts: set[str] = set()
    directions: dict[str, ExplorationDirection] = {}

    def observe(fact: Fact, key: str) -> None:
        if fact_is_effective(fact, objects, now):
            evidence.setdefault(key, fact.ref)
        elif (
            fact.lifecycle == "contested"
            and (fact.valid_from is None or fact.valid_from <= now)
            and (fact.valid_until is None or fact.valid_until > now)
        ):
            conflicts.add(key)

    for fact in sorted((i for i in board.objects if isinstance(i, Fact)), key=lambda i: i.id):
        if not contributes_to(fact, root.id, objects):
            continue
        for criterion in root.acceptance:
            query = FactQuery(
                claim_key=criterion.claim_key,
                claim_type=criterion.claim_type,
                scope=criterion.scope,
            )
            if query_matches(query, fact) and set(criterion.required_checks) <= set(
                fact.verification.checks
            ):
                observe(fact, "goal:" + criterion_key(criterion))

    for intent in board.objects:
        if not isinstance(intent, Intent) or not _related(intent, board, specs):
            continue
        for spec in specs:
            if (
                spec.activation_rule.intent_topics
                and intent.topic not in spec.activation_rule.intent_topics
            ):
                continue
            view = make_view(board, intent, spec, now)
            for query in spec.activation_rule.required_facts:
                key = "input:" + digest({"source": spec.id, "query": query.model_dump(mode="json")})
                for fact in view.objects:
                    if isinstance(fact, Fact) and query_matches(query, fact):
                        observe(fact, key)
            if match_rule(spec, view) is None:
                continue
            # Ignore IDs, revisions, free text and arbitrary extra scope fields. Only declared
            # input slots and their values can distinguish another evidence-backed direction.
            inputs = sorted(
                {
                    digest({"query": q.model_dump(mode="json"), "value": fact.value})
                    for q in spec.activation_rule.required_facts
                    for fact in view.objects
                    if isinstance(fact, Fact)
                    and query_matches(q, fact)
                    and fact_is_effective(fact, objects, now)
                }
            )
            fingerprint = digest(
                {
                    "source": spec.id,
                    "version": spec.version,
                    "strategy": spec.strategy_version,
                    "intent": _intent_key(intent),
                    "inputs": inputs,
                    "rule": spec.activation_rule.model_dump(mode="json", exclude={"reason"}),
                }
            )
            directions.setdefault(
                fingerprint,
                ExplorationDirection(
                    source_id=spec.id,
                    intent_ref=intent.ref,
                    input_fingerprint=digest(inputs),
                ),
            )
    return ProgressSnapshot(
        board_revision=board.revision,
        evaluated_at=now,
        evidence=evidence,
        satisfied=tuple(
            sorted(
                criterion_key(c) for c in root.acceptance if c.id not in evaluation.missing_criteria
            )
        ),
        gaps={
            criterion_key(c): c.description
            for c in root.acceptance
            if c.id in evaluation.missing_criteria
        },
        conflicts=tuple(sorted(conflicts)),
        directions=directions,
    )


def initial(snapshot: ProgressSnapshot, budget: int) -> ProgressState:
    return ProgressState(
        snapshot=snapshot,
        exploration_remaining=budget,
        seen_evidence=tuple(sorted(snapshot.evidence)),
        seen_satisfied=snapshot.satisfied,
        seen_directions=tuple(sorted(snapshot.directions)),
    )


def permits_exploration(
    before: BoardSnapshot,
    after: BoardSnapshot,
    specs: tuple[KnowledgeSourceSpec, ...],
    now: datetime,
) -> bool:
    old = before.by_id()
    objects = after.by_id()
    return any(
        old.get(item.id) != item
        and (
            isinstance(item, Intent)
            and item.status == "open"
            and _related(item, after, specs)
            or isinstance(item, Hint)
            and item.status == "active"
            and (item.expires_at is None or item.expires_at > now)
            and contributes_to(item, after.root_intent_id, objects)
        )
        for item in after.objects
    )


def advance(
    state: ProgressState,
    after: ProgressSnapshot,
    *,
    allow_exploration: bool,
) -> tuple[ProgressState, ProgressDecision]:
    before = state.snapshot
    new_evidence = set(after.evidence) - set(before.evidence) - set(state.seen_evidence)
    closed = set(after.satisfied) - set(before.satisfied) - set(state.seen_satisfied)
    # Merely deleting/retracting both sides of a conflict is not resolution evidence.
    resolved = (set(before.conflicts) - set(after.conflicts)) & set(after.evidence)
    resolved -= set(state.seen_resolutions)
    opened = set(after.directions) - set(before.directions) - set(state.seen_directions)
    remaining = state.exploration_remaining
    reasons = []
    if new_evidence:
        reasons.append("relevant_evidence_added")
    if closed:
        reasons.append("acceptance_gap_closed")
    if resolved:
        reasons.append("conflict_resolved_with_evidence")
    if reasons:
        kind = "goal_progress"
    elif allow_exploration and opened and remaining:
        kind = "exploration"
        reasons.append("new_executable_direction")
        remaining -= 1
    else:
        kind = "no_progress"
        reasons.append(
            "exploration_budget_exhausted"
            if allow_exploration and opened
            else "no_new_goal_evidence_or_direction"
        )
    decision = ProgressDecision(
        kind=kind,
        before_revision=before.board_revision,
        after_revision=after.board_revision,
        reasons=tuple(reasons),
        new_evidence=tuple(sorted(new_evidence)),
        closed_gaps=tuple(sorted(closed)),
        resolved_conflicts=tuple(sorted(resolved)),
        new_directions=tuple(sorted(opened)) if allow_exploration else (),
    )
    return ProgressState(
        snapshot=after,
        exploration_remaining=remaining,
        seen_evidence=tuple(sorted(set(state.seen_evidence) | set(after.evidence))),
        seen_satisfied=tuple(sorted(set(state.seen_satisfied) | set(after.satisfied))),
        seen_resolutions=tuple(sorted(set(state.seen_resolutions) | resolved)),
        seen_directions=tuple(sorted(set(state.seen_directions) | set(after.directions))),
        last_decision=decision,
    ), decision
