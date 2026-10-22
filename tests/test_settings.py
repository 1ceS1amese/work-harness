import asyncio
import json
from pathlib import Path

import pytest

from work_harness.config.loader import load_app_config
from work_harness.prompts import PromptRegistry
from work_harness.storage import (
    ApplicationSettings,
    ApplicationSettingsStore,
    EditableRuntimeSettings,
    apply_application_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_application_settings_store_round_trips_prompt_and_runtime(
    tmp_path: Path,
) -> None:
    config = load_app_config(PROJECT_ROOT)
    runtime = EditableRuntimeSettings.from_runtime(config.runtime).model_copy(
        update={"run_max_concurrency": 2}
    )
    expected = ApplicationSettings(
        prompts={"planner": "Use a short, bounded plan."},
        runtime=runtime,
    )
    path = tmp_path / "settings.json"
    store = ApplicationSettingsStore(path)
    await store.initialize()
    await store.save(expected)

    reopened = ApplicationSettingsStore(path)
    await reopened.initialize()

    assert await reopened.get() == expected
    assert path.stat().st_mode & 0o777 == 0o600
    applied = apply_application_settings(config, expected)
    assert applied.runtime.run_max_concurrency == 2


@pytest.mark.asyncio
async def test_application_settings_loads_file_from_before_queue_fields(
    tmp_path: Path,
) -> None:
    config = load_app_config(PROJECT_ROOT)
    legacy_runtime = EditableRuntimeSettings.from_runtime(config.runtime).model_dump(
        mode="json"
    )
    legacy_runtime.pop("run_queue_capacity")
    legacy_runtime.pop("run_queue_retry_after_seconds")
    path = tmp_path / "legacy-settings.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "prompts": {}, "runtime": legacy_runtime},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = ApplicationSettingsStore(path)
    await store.initialize()
    loaded = await store.get()

    assert loaded.runtime is not None
    assert loaded.runtime.run_queue_capacity == 8
    assert loaded.runtime.run_queue_retry_after_seconds == 5


def test_prompt_registry_uses_persisted_override_without_modifying_source() -> None:
    source = (PROJECT_ROOT / "prompts" / "planner.md").read_text(encoding="utf-8")
    registry = PromptRegistry(
        PROJECT_ROOT / "prompts",
        overrides={"planner": "Build one carefully bounded plan."},
    )

    prompt = registry.get("planner")

    assert prompt.body == "Build one carefully bounded plan."
    assert prompt.version.endswith("+custom")
    assert (PROJECT_ROOT / "prompts" / "planner.md").read_text(encoding="utf-8") == source


@pytest.mark.asyncio
async def test_settings_updates_merge_against_the_latest_persisted_value(
    tmp_path: Path,
) -> None:
    config = load_app_config(PROJECT_ROOT)
    runtime = EditableRuntimeSettings.from_runtime(config.runtime).model_copy(
        update={"run_max_concurrency": 2}
    )
    store = ApplicationSettingsStore(tmp_path / "settings.json")
    await store.initialize()

    def add_prompt(current: ApplicationSettings) -> ApplicationSettings:
        return ApplicationSettings(
            prompts={**current.prompts, "planner": "Keep the plan bounded."},
            runtime=current.runtime,
        )

    def change_runtime(current: ApplicationSettings) -> ApplicationSettings:
        return ApplicationSettings(prompts=current.prompts, runtime=runtime)

    await asyncio.gather(store.update(add_prompt), store.update(change_runtime))
    saved = await store.get()

    assert saved.prompts == {"planner": "Keep the plan bounded."}
    assert saved.runtime == runtime
