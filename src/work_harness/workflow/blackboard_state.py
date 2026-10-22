"""Root control state contains references, never shared knowledge or private messages."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def merge_completed(
    current: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return list(
        {(item["activation_id"], item["attempt"]): item for item in [*current, *incoming]}.values()
    )


class BlackboardGraphState(TypedDict, total=False):
    board_id: str
    workflow_revision: str
    started_at: str
    observed_revision: int
    batch_id: str | None
    dispatch_items: list[dict[str, Any]]
    wakeup_at: str
    completed_activations: Annotated[list[dict[str, Any]], merge_completed]
    route: str
    status: str
    stop_reason: str
    final_output: str
    finalized_at_revision: int
    pending_intents: list[dict[str, Any]]
