from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from work_harness.observability.logging import get_logger


class TraceRecorder:
    def __init__(self, enabled: bool = True, sample_rate: float = 1.0) -> None:
        self.enabled = enabled
        self.sample_rate = sample_rate
        self.logger = get_logger("work_harness.trace")

    def _sampled(self, run_id: str) -> bool:
        if not self.enabled or self.sample_rate <= 0:
            return False
        if self.sample_rate >= 1:
            return True
        digest = hashlib.blake2s(run_id.encode("utf-8"), digest_size=8).digest()
        fraction = int.from_bytes(digest, "big") / ((1 << 64) - 1)
        return fraction < self.sample_rate

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
        sampled = self._sampled(run_id)
        if sampled:
            self.logger.info(
                "node_started",
                extra={"run_id": run_id, "trace_id": current_trace_id, "node": node, **metadata},
            )
        try:
            yield current_trace_id
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            if sampled:
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
            if sampled:
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
