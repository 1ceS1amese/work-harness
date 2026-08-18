from __future__ import annotations

from typing import Protocol

from work_harness.domain.models import ModelRequest, ModelResponse


class ModelGatewayError(RuntimeError):
    retryable = False


class TransientModelError(ModelGatewayError):
    retryable = True


class AuthenticationModelError(ModelGatewayError):
    pass


class InvalidModelRequestError(ModelGatewayError):
    pass


class ModelProvider(Protocol):
    name: str
    capabilities: frozenset[str]

    async def complete(self, request: ModelRequest, *, model: str) -> ModelResponse: ...

    async def healthcheck(self) -> bool: ...

