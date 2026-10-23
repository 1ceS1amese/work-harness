from __future__ import annotations

import asyncio
import random

from work_harness.config.models import ModelCandidateConfig, ModelsConfig, RuntimeConfig
from work_harness.domain.models import ModelRequest, ModelResponse, ModelSelection
from work_harness.llm.base import ModelGatewayError, ModelProvider, TransientModelError
from work_harness.llm.verification import current_settings
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
        self._provider_slots = {
            name: asyncio.Semaphore(provider.max_concurrency)
            for name, provider in config.providers.items()
            if provider.enabled and name in providers
        }
        self._logger = get_logger("work_harness.model_gateway")

    @property
    def logical_models(self) -> tuple[str, ...]:
        return tuple(self._config.logical_models)

    def model_choices(self) -> list[dict]:
        choices = []
        for name, config in self._config.providers.items():
            if not config.enabled or name not in self._providers:
                continue
            for model in config.allowed_models:
                settings = current_settings(config, model)
                if settings and settings.verification.model_available:
                    choices.append(
                        {
                            "provider": name,
                            "model": model,
                            "reasoning_effort": settings.default_effort,
                            "supported_efforts": settings.verification.supported_efforts,
                        }
                    )
        return choices

    def resolve_selection(
        self, selection: ModelSelection, *, use_default: bool = True
    ) -> ModelSelection:
        config = self._config.providers.get(selection.provider)
        if (
            not config
            or not config.enabled
            or selection.provider not in self._providers
            or selection.model not in config.allowed_models
        ):
            raise ValueError("所选模型尚未加载或已停用，请检查模型设置")
        settings = current_settings(config, selection.model)
        if not settings or not settings.verification.model_available:
            raise ValueError("所选模型尚未通过当前连接的测试，请在设置页测试后保存并重启")
        effort = selection.reasoning_effort
        if use_default and effort is None:
            effort = settings.default_effort
        if effort is not None and effort not in settings.verification.supported_efforts:
            raise ValueError("所选推理档位尚未通过该模型的测试")
        return selection.model_copy(update={"reasoning_effort": effort})

    async def complete(self, request: ModelRequest) -> ModelResponse:
        deadline = asyncio.get_running_loop().time() + request.timeout_seconds
        try:
            # One deadline includes provider admission, retries, backoff and fallback.
            async with asyncio.timeout_at(deadline):
                return await self._complete(request, deadline=deadline)
        except TimeoutError as exc:
            raise ModelGatewayError(
                f"model deadline exceeded for logical model {request.logical_model}"
            ) from exc

    async def _complete(self, request: ModelRequest, *, deadline: float) -> ModelResponse:
        logical = self._config.logical_models.get(request.logical_model)
        if logical is None:
            raise ModelGatewayError(f"unknown logical model: {request.logical_model}")

        last_error: Exception | None = None
        fallback_from: str | None = None
        required_capabilities = request.required_capabilities | logical.required_capabilities
        agent_route = self._config.agent_routes.get(request.agent_role)
        configured_candidates = list(logical.candidates)
        if agent_route is not None:
            configured_candidates = [agent_route] + [
                candidate for candidate in configured_candidates if candidate != agent_route
            ]
        if request.model_selection:
            try:
                selection = self.resolve_selection(request.model_selection, use_default=False)
            except ValueError as exc:
                raise ModelGatewayError(str(exc)) from exc
            configured_candidates = [
                ModelCandidateConfig(provider=selection.provider, model=selection.model)
            ]
            agent_route = None
            if request.provider_allowlist and selection.provider not in request.provider_allowlist:
                raise ModelGatewayError("所选模型服务与任务能力的 Provider 限制冲突")
        candidate_providers = {candidate.provider for candidate in configured_candidates}
        unknown_allowlist = request.provider_allowlist - candidate_providers
        if unknown_allowlist and not request.model_selection:
            raise ModelGatewayError(
                f"provider allowlist is not a candidate for {request.logical_model}: "
                f"{sorted(unknown_allowlist)}"
            )
        unknown_preferences = set(request.provider_preferences) - candidate_providers
        if unknown_preferences and not request.model_selection:
            raise ModelGatewayError(
                f"provider preferences are not candidates for {request.logical_model}: "
                f"{sorted(unknown_preferences)}"
            )

        candidates = [
            candidate
            for candidate in configured_candidates
            if not request.provider_allowlist or candidate.provider in request.provider_allowlist
        ]
        preference_order = {
            provider: index for index, provider in enumerate(request.provider_preferences)
        }
        candidates = sorted(
            enumerate(candidates),
            key=lambda item: (
                0 if agent_route is not None and item[1] == agent_route else 1,
                preference_order.get(item[1].provider, len(preference_order)),
                item[0],
            ),
        )
        for _, candidate in candidates:
            provider_config = self._config.providers[candidate.provider]
            provider = self._providers.get(candidate.provider)
            if not provider_config.enabled or provider is None:
                continue
            if not required_capabilities.issubset(provider_config.capabilities):
                continue
            if not required_capabilities.issubset(provider.capabilities):
                continue

            settings = current_settings(provider_config, candidate.model)
            effort = (
                request.model_selection.reasoning_effort
                if request.model_selection
                else settings.default_effort
                if settings
                else None
            )
            routed_request = request.model_copy(update={"reasoning_effort": effort})
            candidate_name = f"{candidate.provider}/{candidate.model}"
            for attempt in range(self._runtime.model_retry_attempts + 1):
                try:
                    async with asyncio.timeout(provider_config.timeout_seconds):
                        response = await self._complete_with_slot(
                            candidate.provider,
                            provider,
                            routed_request,
                            candidate.model,
                        )
                    return response.model_copy(
                        update={
                            "usage": response.usage.model_copy(
                                update={
                                    "usage_complete": response.usage.usage_complete
                                    and last_error is None,
                                }
                            ),
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
                        delay = random.uniform(0.5, 1.0) * min(0.1 * (2**attempt), 1.0)
                        retry_after = getattr(exc, "retry_after_seconds", None)
                        if retry_after is not None:
                            delay = max(delay, retry_after)
                        if delay >= deadline - asyncio.get_running_loop().time():
                            break  # Try another candidate instead of sleeping past the deadline.
                        await asyncio.sleep(delay)
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

    async def _complete_with_slot(
        self,
        provider_name: str,
        provider: ModelProvider,
        request: ModelRequest,
        model: str,
    ) -> ModelResponse:
        async with self._provider_slots[provider_name]:
            return await provider.complete(request, model=model)

    async def health(self) -> dict[str, bool]:
        async def inspect(name: str, provider: ModelProvider) -> tuple[str, bool]:
            try:
                async with asyncio.timeout(min(2.0, self._config.providers[name].timeout_seconds)):
                    return name, await provider.healthcheck()
            except Exception:
                return name, False

        inspected = await asyncio.gather(
            *(inspect(name, provider) for name, provider in self._providers.items())
        )
        return dict(inspected)

    async def close(self) -> None:
        closers = []
        for provider in self._providers.values():
            close = getattr(provider, "close", None)
            if close is not None:
                closers.append(close())
        if closers:
            await asyncio.gather(*closers)
