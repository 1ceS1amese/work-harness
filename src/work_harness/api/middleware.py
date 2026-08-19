from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any


class _RequestBodyTooLarge(Exception):
    pass


@dataclass(slots=True)
class _TokenBucket:
    tokens: float
    updated_at: float


class RequestGuardMiddleware:
    """Small ASGI-native body and per-client rate guard for public API routes."""

    def __init__(
        self,
        app: Any,
        *,
        max_body_bytes: int,
        rate_limit_per_minute: int,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.rate_limit_per_minute = rate_limit_per_minute
        self._buckets: dict[str, _TokenBucket] = {}
        self._bucket_lock = asyncio.Lock()
        self._request_count = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/"):
            await self.app(scope, receive, send)
            return

        content_length = self._content_length(scope)
        if content_length is not None and content_length > self.max_body_bytes:
            await self._send_json(send, 413, "request body is too large")
            return

        client = scope.get("client")
        client_key = str(client[0]) if client else "unknown"
        admitted, retry_after = await self._admit(client_key)
        if not admitted:
            await self._send_json(
                send,
                429,
                "rate limit exceeded",
                headers=[(b"retry-after", str(retry_after).encode("ascii"))],
            )
            return

        received = 0
        response_started = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await self._send_json(send, 413, "request body is too large")

    @staticmethod
    def _content_length(scope: dict[str, Any]) -> int | None:
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                return int(value)
            except ValueError:
                return None
        return None

    async def _admit(self, client_key: str) -> tuple[bool, int]:
        now = time.monotonic()
        capacity = float(self.rate_limit_per_minute)
        refill_per_second = capacity / 60
        async with self._bucket_lock:
            bucket = self._buckets.get(client_key)
            if bucket is None:
                bucket = _TokenBucket(tokens=capacity, updated_at=now)
                self._buckets[client_key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_per_second)
                bucket.updated_at = now

            self._request_count += 1
            if self._request_count % 256 == 0:
                stale_before = now - 120
                self._buckets = {
                    key: value
                    for key, value in self._buckets.items()
                    if value.updated_at >= stale_before
                }

            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return True, 0
            retry_after = max(1, int((1 - bucket.tokens) / refill_per_second) + 1)
            return False, retry_after

    @staticmethod
    async def _send_json(
        send: Any,
        status_code: int,
        detail: str,
        *,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(
            {"detail": detail},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response_headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if headers:
            response_headers.extend(headers)
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": response_headers,
            }
        )
        await send({"type": "http.response.body", "body": body})
