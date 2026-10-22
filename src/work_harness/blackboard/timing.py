"""Durable wake-up conditions derived from the board and the execution ledger."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, Field

from work_harness.blackboard.models import BoardModel, Fact, Hint
from work_harness.blackboard.protocol import BoardSnapshot

if TYPE_CHECKING:
    from work_harness.blackboard.execution import ActivationRecord


class WakeCause(BoardModel):
    kind: Literal[
        "fact_start", "fact_expiry", "hint_expiry", "retry", "attempt_deadline", "run_deadline"
    ]
    subject_id: str
    revision_or_attempt: int | None = None


class WakeupPlan(BoardModel):
    board_revision: int = Field(ge=1)
    evaluated_at: AwareDatetime
    next_at: AwareDatetime
    causes: tuple[WakeCause, ...]
    # A run deadline alone is not a reason to keep an otherwise blocked run alive.
    waitable: bool


def next_wakeup(
    snapshot: BoardSnapshot,
    records: tuple[ActivationRecord, ...],
    *,
    now: datetime,
    deadline: datetime,
    retry_ids: frozenset[str] | None = None,
) -> WakeupPlan | None:
    if snapshot.finalization is not None or now >= deadline:
        return None
    conditions: list[tuple[datetime, WakeCause]] = [
        (deadline, WakeCause(kind="run_deadline", subject_id=snapshot.board_id))
    ]

    def add(at: datetime | None, kind: str, identity: str, revision: int) -> None:
        if at is not None and now < at <= deadline:
            conditions.append(
                (at, WakeCause(kind=kind, subject_id=identity, revision_or_attempt=revision))
            )

    for item in snapshot.objects:
        if isinstance(item, Fact) and item.lifecycle in {"valid", "contested", "stale"}:
            if item.lifecycle == "valid":
                add(item.valid_from, "fact_start", item.id, item.revision)
            add(item.valid_until, "fact_expiry", item.id, item.revision)
        elif isinstance(item, Hint) and item.status == "active":
            add(item.expires_at, "hint_expiry", item.id, item.revision)
    for record in records:
        if record.status == "running":
            add(record.deadline, "attempt_deadline", record.candidate.id, record.attempt)
        elif (
            record.status == "retryable"
            and record.attempt < record.spec.limits.max_attempts
            and (retry_ids is None or record.candidate.id in retry_ids)
        ):
            add(record.retry_at, "retry", record.candidate.id, record.attempt)
    at = min(time for time, _ in conditions)
    causes = tuple(
        sorted(
            (cause for time, cause in conditions if time == at),
            key=lambda cause: (cause.kind, cause.subject_id),
        )
    )
    return WakeupPlan(
        board_revision=snapshot.revision,
        evaluated_at=now,
        next_at=at,
        causes=causes,
        waitable=any(
            cause.kind in {"fact_start", "fact_expiry", "hint_expiry", "retry"}
            for _, cause in conditions
        ),
    )
