from __future__ import annotations

import asyncio

from work_harness.config.models import ModelsConfig, RuntimeConfig
from work_harness.domain.models import ModelRequest, ModelResponse
from work_harness.llm.base import ModelGatewayError, ModelProvider, TransientModelError
from work_harness.observability.logging import get_logger


class ModelGateway:
    """Deterministic logical-model router with bounded retries and fallback."""

    def __init__(
        self,
        config: ModelsConfig,
        providers: dict[str, ModelProvider],
        runtime: RuntimeConfig,
    ) -> None:
        self._config = config
        self._providers = providers
        self._runtime = runtime
        self._logger = get_logger("work_harness.model_gateway")

    @property
    def logical_models(self) -> tuple[str, ...]:
        return tuple(self._config.logical_models)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        logical = self._config.logical_models.get(request.logical_model)
        if logical is None:
            raise ModelGatewayError(f"unknown logical model: {request.logical_model}")

        last_error: Exception | None = None
        fallback_from: str | None = None
        required_capabilities = (
            request.required_capabilities | logical.required_capabilities
        )
        for candidate in logical.candidates:
            provider_config = self._config.providers[candidate.provider]
            provider = self._providers.get(candidate.provider)
            if not provider_config.enabled or provider is None:
                continue
            if not required_capabilities.issubset(provider_config.capabilities):
                continue
            if not required_capabilities.issubset(provider.capabilities):
                continue

            candidate_name = f"{candidate.provider}/{candidate.model}"
            for attempt in range(self._runtime.model_retry_attempts + 1):
                try:
                    response = await asyncio.wait_for(
                        provider.complete(request, model=candidate.model),
                        timeout=min(request.timeout_seconds, provider_config.timeout_seconds),
                    )
                    return response.model_copy(
                        update={
                            "retries": attempt,
                            "fallback_from": fallback_from,
                        }
                    )
                except (TimeoutError, TransientModelError) as exc:
                    last_error = exc
                    self._logger.warning(
                        "transient_model_error",
                        extra={
                            "run_id": request.run_id,
                            "trace_id": request.trace_id,
                            "node": request.node_name,
                            "provider": candidate.provider,
                            "model": candidate.model,
                            "attempt": attempt + 1,
                        },
                    )
                    if attempt < self._runtime.model_retry_attempts:
                        await asyncio.sleep(min(0.1 * (2**attempt), 1.0))
                        continue
                    break
                except ModelGatewayError as exc:
                    last_error = exc
                    break
            fallback_from = fallback_from or candidate_name

        if last_error:
            raise ModelGatewayError(
                f"all candidates failed for logical model {request.logical_model}: {last_error}"
            ) from last_error
        raise ModelGatewayError(
            f"no enabled provider satisfies capabilities for {request.logical_model}: "
            f"{sorted(required_capabilities)}"
        )

    async def health(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for name, provider in self._providers.items():
            try:
                results[name] = await provider.healthcheck()
            except Exception:
                results[name] = False
        return results

    async def close(self) -> None:
        for provider in self._providers.values():
            close = getattr(provider, "close", None)
            if close is not None:
                await close()
