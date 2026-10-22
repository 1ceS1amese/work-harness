"""Real Worker process used only by PostgreSQL fault-injection tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from work_harness.llm.mock import MockProvider
from work_harness.llm.registry import ProviderAdapterRegistry
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.storage.blackboard_execution_postgres import PostgresActivationStore

ROOT = Path(__file__).resolve().parents[2]


async def main():
    marker = os.getenv("BLACKBOARD_TEST_PAUSE_BEFORE_COMMIT")
    if marker:
        original = PostgresActivationStore.commit_activation

        async def pause(self, request, *args, **kwargs):
            await asyncio.to_thread(Path(marker).write_text, request.board_id)
            # A signal kills this owned test process; no production pause switch exists.
            await asyncio.Event().wait()
            return await original(self, request, *args, **kwargs)

        PostgresActivationStore.commit_activation = pause
    providers = ProviderAdapterRegistry()
    providers.register("openai_compatible", lambda name, _: MockProvider(name))
    async with build_runtime(ROOT, provider_adapters=providers) as runtime:
        manager = RunManager(runtime)
        try:
            await manager.initialize()
            await asyncio.to_thread(
                Path(os.environ["BLACKBOARD_TEST_READY_FILE"]).write_text, manager._owner
            )
            await manager.wait_worker()
        finally:
            await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
