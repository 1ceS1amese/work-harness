"""Read-only blackboard routes and durable event streaming."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, TypeVar
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from work_harness.blackboard.models import ObjectKind
from work_harness.blackboard.protocol import (
    BlackboardError,
    BoardNotFoundError,
    HistoryUnavailableError,
)
from work_harness.blackboard.query import (
    BlackboardQuery,
    ContentDetail,
    EventPage,
    HistoryPage,
    ObjectDetail,
    ObjectFilter,
    ObjectPage,
    ObjectStatus,
    QueryConflictError,
    QueryNotFoundError,
    ReadCursor,
    SnapshotSummary,
    event_cursor,
)
from work_harness.service import RunManager
from work_harness.storage.base import RunStoreNotFoundError
from work_harness.storage.blackboard_factory import build_activation_store

Revision = Annotated[int | None, Query(ge=1)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=16384)]
Limit = Annotated[int, Query(ge=1, le=200)]
_Result = TypeVar("_Result")


def snapshot_required(run_id: str, reason: str) -> dict[str, str]:
    return {
        "code": "snapshot_required",
        "reason": reason,
        "snapshot_url": f"/v1/runs/{quote(run_id, safe='')}/blackboard",
    }


async def read_result(result: Awaitable[_Result], run_id: str) -> _Result:
    try:
        return await result
    except BoardNotFoundError as exc:
        raise HTTPException(409, detail={"code": "blackboard_not_initialized"}) from exc
    except HistoryUnavailableError as exc:
        raise HTTPException(410, detail=snapshot_required(run_id, str(exc))) from exc
    except QueryNotFoundError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except QueryConflictError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except BlackboardError:
        raise
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


async def board_query(manager: RunManager, run_id: str) -> BlackboardQuery:
    try:
        record = await manager.runtime.run_store.get(run_id)
    except RunStoreNotFoundError as exc:
        raise HTTPException(404, detail="run not found") from exc
    if record is None:
        raise HTTPException(404, detail="run not found")
    if record.engine != "blackboard":
        raise HTTPException(409, detail="this run uses the legacy engine")
    store = await manager.runtime.blackboard_store()
    ledger = build_activation_store(store)
    ledger.bind(store)
    return BlackboardQuery(store, run_id, ledger=ledger)


def blackboard_router(manager: Callable[[], RunManager]) -> APIRouter:
    router = APIRouter(prefix="/v1/runs/{run_id}/blackboard", tags=["blackboard"])

    @router.get("", response_model=SnapshotSummary)
    async def summary(run_id: str, at_revision: Revision = None) -> SnapshotSummary:
        query = await board_query(manager(), run_id)
        return await read_result(query.summary(at_revision=at_revision), run_id)

    @router.get("/objects", response_model=ObjectPage)
    async def objects(
        run_id: str,
        kind: ObjectKind | None = None,
        status: ObjectStatus | None = None,
        topic: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        intent_id: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        at_revision: Revision = None,
        cursor: Cursor = None,
        limit: Limit = 50,
    ) -> ObjectPage:
        query = await board_query(manager(), run_id)
        try:
            filters = ObjectFilter(kind=kind, status=status, topic=topic, intent_id=intent_id)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return await read_result(
            query.objects(
                filters=filters,
                at_revision=at_revision,
                cursor=cursor,
                limit=limit,
            ),
            run_id,
        )

    @router.get("/objects/{object_id}", response_model=ObjectDetail)
    async def object_detail(
        run_id: str,
        object_id: str,
        at_revision: Revision = None,
        object_revision: Revision = None,
    ) -> ObjectDetail:
        query = await board_query(manager(), run_id)
        return await read_result(
            query.object(
                object_id,
                at_revision=at_revision,
                object_revision=object_revision,
            ),
            run_id,
        )

    @router.get("/objects/{object_id}/revisions", response_model=HistoryPage)
    async def history(
        run_id: str,
        object_id: str,
        at_revision: Revision = None,
        cursor: Cursor = None,
        limit: Limit = 50,
    ) -> HistoryPage:
        query = await board_query(manager(), run_id)
        return await read_result(
            query.history(
                object_id,
                at_revision=at_revision,
                cursor=cursor,
                limit=limit,
            ),
            run_id,
        )

    @router.get("/events", response_model=EventPage)
    async def events(run_id: str, after: Cursor = None, limit: Limit = 100) -> EventPage:
        query = await board_query(manager(), run_id)
        return await read_result(query.events(after=after, limit=limit), run_id)

    @router.get("/content/{digest}", response_model=ContentDetail)
    async def content(run_id: str, digest: str, at_revision: Revision = None) -> ContentDetail:
        query = await board_query(manager(), run_id)
        return await read_result(query.content(digest, at_revision=at_revision), run_id)

    return router


async def stream_blackboard(
    manager: RunManager,
    run_id: str,
    request: Request,
    *,
    after: str | None = None,
    last_event_id: str | None = None,
) -> StreamingResponse:
    query = await board_query(manager, run_id)
    try:
        cursors = [
            ReadCursor.decode(token, board_id=run_id, kind="events")
            for token in (after, last_event_id)
            if token is not None
        ]
        if len(cursors) == 2 and cursors[0].revision != cursors[1].revision:
            raise ValueError("after and Last-Event-ID disagree")
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    position = last_event_id or after
    try:
        initial = await query.events(after=position)
    except BoardNotFoundError as exc:
        if cursors and cursors[0].revision > 0:
            raise HTTPException(
                410, detail=snapshot_required(run_id, "board is unavailable")
            ) from exc
        initial = None
    except HistoryUnavailableError as exc:
        raise HTTPException(410, detail=snapshot_required(run_id, str(exc))) from exc
    except QueryConflictError as exc:
        raise HTTPException(409, detail=str(exc)) from exc

    async def events() -> AsyncIterator[str]:
        cursor = position
        page = initial
        loop = asyncio.get_running_loop()
        keepalive = manager.runtime.config.runtime.sse_keepalive_seconds
        last_write = loop.time()
        inactive_seen = False
        async with manager.subscribe(run_id) as updates:
            while not await request.is_disconnected():
                try:
                    page = page if page is not None else await query.events(after=cursor)
                except BoardNotFoundError:
                    if (
                        cursor is not None
                        and ReadCursor.decode(cursor, board_id=run_id, kind="events").revision > 0
                    ):
                        detail = snapshot_required(run_id, "board is unavailable")
                        yield f"event: snapshot_required\ndata: {json.dumps(detail)}\n\n"
                        return
                    page = None
                except (HistoryUnavailableError, QueryConflictError) as exc:
                    detail = snapshot_required(run_id, str(exc))
                    yield f"event: snapshot_required\ndata: {json.dumps(detail)}\n\n"
                    return
                if page is not None:
                    for event in page.items:
                        cursor = event_cursor(run_id, event.sequence)
                        payload = event.model_dump_json()
                        yield f"id: {cursor}\nevent: blackboard\ndata: {payload}\n\n"
                        last_write = loop.time()
                    cursor = page.next_cursor
                    if page.has_more:
                        page = None
                        continue
                    if page.finalization is not None:
                        return
                record = await manager.runtime.run_store.get(run_id)
                if record is None:
                    return
                if record.status not in {"pending", "running"}:
                    # A final commit may have arrived between the durable read and this index read.
                    if not inactive_seen:
                        inactive_seen = True
                        page = None
                        continue
                    return
                inactive_seen = False
                page = None
                if loop.time() - last_write >= keepalive:
                    yield ": keepalive\n\n"
                    last_write = loop.time()
                try:
                    # Notifications are an optimization; polling covers lost or absent wakeups.
                    await asyncio.wait_for(updates.get(), timeout=min(1.0, keepalive))
                except TimeoutError:
                    pass

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
