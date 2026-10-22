from __future__ import annotations

from datetime import timedelta

import pytest

from work_harness.domain.enums import RunJobStatus, RunStatus
from work_harness.domain.models import utc_now
from work_harness.storage import (
    MemoryRunStore,
    RunQueueCapacityError,
    RunQueueStateError,
    RunRecord,
)


@pytest.mark.asyncio
async def test_submit_is_atomic_idempotent_and_globally_bounded() -> None:
    store = MemoryRunStore()
    await store.initialize()
    original = RunRecord(
        run_id="run_original",
        idempotency_key_hash="a" * 64,
        request_fingerprint="b" * 64,
    )

    submitted = await store.submit(
        original,
        {"goal": "persist me"},
        capacity=1,
        max_attempts=3,
    )
    duplicate = await store.submit(
        RunRecord(
            run_id="run_duplicate",
            idempotency_key_hash="a" * 64,
            request_fingerprint="b" * 64,
        ),
        {"goal": "persist me"},
        capacity=1,
        max_attempts=3,
    )

    assert submitted.created is True
    assert submitted.job is not None
    assert duplicate.created is False
    assert duplicate.record == original
    assert duplicate.job is None
    with pytest.raises(RunQueueCapacityError):
        await store.submit(
            RunRecord(run_id="run_overflow"),
            {"goal": "too many"},
            capacity=1,
            max_attempts=3,
        )
    assert [item.run_id for item in await store.list()] == ["run_original"]


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_old_fencing_token_is_rejected() -> None:
    store = MemoryRunStore()
    await store.initialize()
    submission = await store.submit(
        RunRecord(run_id="run_fenced"),
        {"goal": "lease me"},
        capacity=2,
        max_attempts=3,
    )
    first = await store.claim_job(owner="worker-a", lease_seconds=30)
    assert first is not None
    assert submission.job is not None
    expired = first.model_copy(
        update={"lease_expires_at": utc_now() - timedelta(seconds=1)}
    )
    store._jobs[expired.job_id] = expired  # Simulate database clock passing the lease.

    second = await store.claim_job(owner="worker-b", lease_seconds=30)

    assert second is not None
    assert second.job_id == first.job_id
    assert second.attempt_count == 2
    assert second.lease_token != first.lease_token
    assert first.lease_token is not None
    assert second.lease_token is not None
    assert await store.heartbeat_job(
        first.job_id,
        owner="worker-a",
        lease_token=first.lease_token,
        lease_seconds=30,
    ) is False
    assert await store.complete_job(
        first.job_id,
        owner="worker-a",
        lease_token=first.lease_token,
        run_status=RunStatus.SUCCEEDED,
    ) is False
    assert await store.complete_job(
        second.job_id,
        owner="worker-b",
        lease_token=second.lease_token,
        run_status=RunStatus.SUCCEEDED,
    ) is True


@pytest.mark.asyncio
async def test_retry_uses_available_state_then_dead_letters_at_attempt_limit() -> None:
    store = MemoryRunStore()
    await store.initialize()
    await store.submit(
        RunRecord(run_id="run_retry"),
        {"goal": "retry me"},
        capacity=2,
        max_attempts=2,
    )

    first = await store.claim_job(owner="worker-a", lease_seconds=30)
    assert first is not None and first.lease_token is not None
    retry = await store.retry_job(
        first.job_id,
        owner="worker-a",
        lease_token=first.lease_token,
        error_code="database_unavailable",
        error_message="temporary failure",
        delay_seconds=0,
    )
    assert retry is not None
    assert retry.status == RunJobStatus.AVAILABLE

    second = await store.claim_job(owner="worker-b", lease_seconds=30)
    assert second is not None and second.lease_token is not None
    dead = await store.retry_job(
        second.job_id,
        owner="worker-b",
        lease_token=second.lease_token,
        error_code="database_unavailable",
        error_message="still unavailable",
        delay_seconds=0,
    )

    assert dead is not None
    assert dead.status == RunJobStatus.DEAD
    record = await store.get("run_retry")
    assert record is not None
    assert record.status == RunStatus.FAILED
    assert await store.cancel_jobs("run_retry") == 0
    assert (await store.get("run_retry")).status == RunStatus.FAILED
    assert (await store.queue_stats()).dead_jobs == 1
    dead_jobs = await store.list_dead_jobs()
    assert [item.job_id for item in dead_jobs] == [dead.job_id]
    replay = await store.replay_dead_job(
        dead.job_id,
        capacity=2,
        max_attempts=3,
    )
    assert replay.job_id != dead.job_id
    assert replay.payload == dead.payload
    assert replay.status == RunJobStatus.AVAILABLE
    assert (await store.queue_stats()).dead_jobs == 1


@pytest.mark.asyncio
async def test_resume_has_one_active_job_and_cancel_invalidates_lease() -> None:
    store = MemoryRunStore()
    await store.initialize()
    await store.reserve(
        RunRecord(run_id="run_waiting", status=RunStatus.WAITING_HUMAN)
    )
    resume = await store.enqueue_resume(
        "run_waiting",
        {"approved": True, "note": "continue"},
        capacity=2,
        max_attempts=3,
    )
    queued = await store.get("run_waiting")
    assert queued is not None and queued.status == RunStatus.PENDING
    with pytest.raises(RunQueueStateError, match="active job"):
        await store.enqueue_resume(
            "run_waiting",
            {"approved": True},
            capacity=2,
            max_attempts=3,
        )

    claimed = await store.claim_job(owner="worker-a", lease_seconds=30)
    assert claimed is not None and claimed.job_id == resume.job_id
    assert claimed.lease_token is not None
    assert await store.cancel_jobs("run_waiting") == 1
    assert await store.heartbeat_job(
        claimed.job_id,
        owner="worker-a",
        lease_token=claimed.lease_token,
        lease_seconds=30,
    ) is False
    stats = await store.queue_stats()
    assert stats.cancelled_jobs == 1
    assert stats.leased_jobs == 0


@pytest.mark.asyncio
async def test_worker_only_claims_matching_graph_revision() -> None:
    store = MemoryRunStore()
    await store.initialize()
    revision = "a" * 64
    await store.submit(
        RunRecord(run_id="run_revision"),
        {"goal": "revision-bound"},
        capacity=2,
        max_attempts=3,
        graph_revision=revision,
    )

    assert await store.claim_job(
        owner="wrong-worker",
        lease_seconds=30,
        graph_revision="b" * 64,
    ) is None
    assert (
        await store.queue_stats(graph_revision="b" * 64)
    ).incompatible_active_jobs == 1
    claimed = await store.claim_job(
        owner="matching-worker",
        lease_seconds=30,
        graph_revision=revision,
    )
    assert claimed is not None
    assert claimed.graph_revision == revision


@pytest.mark.asyncio
async def test_expired_lease_is_dead_lettered_after_attempt_limit() -> None:
    store = MemoryRunStore()
    await store.initialize()
    await store.submit(
        RunRecord(run_id="run_crash_loop"),
        {"goal": "never retry forever"},
        capacity=2,
        max_attempts=1,
    )
    claimed = await store.claim_job(owner="worker-a", lease_seconds=30)
    assert claimed is not None
    store._jobs[claimed.job_id] = claimed.model_copy(
        update={"lease_expires_at": utc_now() - timedelta(seconds=1)}
    )

    reconciliation = await store.claim_job(owner="worker-b", lease_seconds=30)
    assert reconciliation is not None and reconciliation.reconciliation_only
    assert reconciliation.lease_token is not None
    dead_job = await store.retry_job(
        reconciliation.job_id,
        owner="worker-b",
        lease_token=reconciliation.lease_token,
        error_code="queue_attempts_exhausted",
        error_message="checkpoint was not terminal",
        delay_seconds=0,
    )
    assert dead_job is not None and dead_job.status == RunJobStatus.DEAD
    dead = await store.list_dead_jobs()
    assert [item.job_id for item in dead] == [claimed.job_id]
    assert dead[0].last_error_code == "queue_attempts_exhausted"
    record = await store.get(claimed.run_id)
    assert record is not None and record.status == RunStatus.FAILED
