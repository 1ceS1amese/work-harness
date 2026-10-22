"""Writer ownership and terminal snapshots, separate from board knowledge."""

from __future__ import annotations

from datetime import datetime

from work_harness.blackboard.goals import evaluate
from work_harness.blackboard.protocol import (
    BoardClosedError,
    BoardControl,
    BoardEvent,
    BoardFinalization,
    BoardSnapshot,
    GoalNotSatisfiedError,
    StaleWriterError,
    TerminalStatus,
    WriterToken,
)


def claim_writer(
    control: BoardControl, owner_id: str, session_id: str, *, takeover: bool
) -> tuple[BoardControl, WriterToken]:
    if control.finalization is not None or control.cancelled:
        raise BoardClosedError("blackboard is already closed")
    if control.owner_id is not None and control.session_id == session_id:
        if control.owner_id != owner_id and not takeover:
            raise StaleWriterError("blackboard already has a different writer")
        if control.owner_id == owner_id and not takeover:
            return control, WriterToken(
                board_id=control.board_id,
                owner_id=owner_id,
                session_id=session_id,
                generation=control.generation,
            )
    updated = control.model_copy(
        update={
            "owner_id": owner_id,
            "session_id": session_id,
            "generation": control.generation + 1,
        }
    )
    return updated, WriterToken(
        board_id=control.board_id,
        owner_id=owner_id,
        session_id=session_id,
        generation=updated.generation,
    )


def check_writer(control: BoardControl, token: WriterToken, session_id: str) -> None:
    if control.cancelled or control.finalization is not None:
        raise BoardClosedError("blackboard is already closed")
    if (
        token.board_id != control.board_id
        or token.owner_id != control.owner_id
        or token.generation != control.generation
        or token.session_id != control.session_id
        or token.session_id != session_id
    ):
        raise StaleWriterError("blackboard write token is no longer current")


def release_writer(control: BoardControl, token: WriterToken, session_id: str) -> BoardControl:
    """Relinquish only this generation; retries cannot revoke a later owner's token."""
    try:
        check_writer(control, token, session_id)
    except (StaleWriterError, BoardClosedError):
        return control
    return control.model_copy(
        update={
            "generation": control.generation + 1,
            "owner_id": None,
            "session_id": None,
        }
    )


def seal_snapshot(
    snapshot: BoardSnapshot,
    status: TerminalStatus,
    reason: str,
    now: datetime,
    attempted_directions: tuple[str, ...] = (),
) -> tuple[BoardSnapshot, BoardEvent, BoardFinalization]:
    objects = snapshot.by_id()
    root = objects[snapshot.root_intent_id]
    result = evaluate(root, objects, now)
    if status == "succeeded" and (root.status != "satisfied" or not result.satisfied):
        raise GoalNotSatisfiedError("root goal has no current, valid acceptance evidence")
    lines = [objects[ref.object_id].statement for ref in result.witnesses]
    if status == "cancelled":
        lines.append("任务已取消。")
    if status != "succeeded" and result.missing_criteria:
        lines.append("尚未满足的条件：" + "、".join(result.missing_criteria))
    if result.conflicts:
        lines.append("仍有待解决的事实冲突。")
    if status != "succeeded" and attempted_directions:
        lines.append("已尝试方向：" + "；".join(attempted_directions))
    final = BoardFinalization(
        board_id=snapshot.board_id,
        status=status,
        reason=reason,
        finalized_at_revision=snapshot.revision + 1,
        finalized_at=now,
        output="\n".join(lines),
        witnesses=result.witnesses,
        missing_criteria=result.missing_criteria,
        attempted_directions=attempted_directions,
    )
    updated = snapshot.model_copy(
        update={"revision": final.finalized_at_revision, "finalization": final}
    )
    event = BoardEvent(
        board_id=snapshot.board_id,
        sequence=updated.revision,
        source_id="blackboard.controller",
        committed_at=now,
        changed_refs=(),
        event_type="finalized",
        finalization=final,
    )
    return updated, event, final


def close_control(control: BoardControl, final: BoardFinalization) -> BoardControl:
    return control.model_copy(
        update={
            "generation": control.generation + 1,
            "owner_id": None,
            "session_id": None,
            "cancelled": final.status == "cancelled",
            "finalization": final,
            "wakeup_revision": 0,
            "status_pending": True,
        }
    )
