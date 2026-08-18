from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from work_harness.observability.logging import get_logger


class TraceRecorder:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.logger = get_logger("work_harness.trace")

    @asynccontextmanager
    async def span(
        self,
        *,
        run_id: str,
        node: str,
        trace_id: str | None = None,
        **metadata: Any,
    ) -> AsyncIterator[str]:
        current_trace_id = trace_id or f"trace_{uuid4().hex}"
        started = time.perf_counter()
        if self.enabled:
            self.logger.info(
                "node_started",
                extra={"run_id": run_id, "trace_id": current_trace_id, "node": node, **metadata},
            )
        try:
            yield current_trace_id
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            if self.enabled:
                self.logger.exception(
                    "node_failed",
                    extra={
                        "run_id": run_id,
                        "trace_id": current_trace_id,
                        "node": node,
                        "status": "failed",
                        "duration_ms": duration_ms,
                        **metadata,
                    },
                )
            raise
        else:
            duration_ms = int((time.perf_counter() - started) * 1000)
            if self.enabled:
                self.logger.info(
                    "node_completed",
                    extra={
                        "run_id": run_id,
                        "trace_id": current_trace_id,
                        "node": node,
                        "status": "completed",
                        "duration_ms": duration_ms,
                        **metadata,
                    },
                )

