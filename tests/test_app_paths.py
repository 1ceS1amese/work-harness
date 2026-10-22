from __future__ import annotations

import sys
from pathlib import Path

from work_harness.app_paths import prepare_application_environment, user_data_root
from work_harness.config.loader import default_project_root


def test_windows_application_data_path_uses_local_app_data(tmp_path: Path) -> None:
    environment = {"LOCALAPPDATA": str(tmp_path / "Local")}

    result = user_data_root(environment, platform_name="win32", home=tmp_path)

    assert result == (tmp_path / "Local" / "WorkHarness").resolve()


def test_application_data_override_and_store_defaults(tmp_path: Path) -> None:
    root = tmp_path / "custom-data"
    environment = {
        "WORK_HARNESS_DATA_ROOT": str(root),
        "WORK_HARNESS_RUN_STORE_PATH": str(tmp_path / "explicit-runs.sqlite"),
    }

    paths = prepare_application_environment(environment, platform_name="linux", home=tmp_path)

    assert paths.root == root.resolve()
    assert paths.data.is_dir()
    assert paths.logs.is_dir()
    assert paths.reports.is_dir()
    assert paths.exports.is_dir()
    assert environment["WORK_HARNESS_PROFILE"] == "setup"
    assert environment["WORK_HARNESS_CHECKPOINT_PATH"] == str(
        root / "data" / "checkpoints.sqlite"
    )
    assert environment["WORK_HARNESS_BLACKBOARD_PATH"] == str(
        root / "data" / "blackboard.sqlite"
    )
    assert environment["WORK_HARNESS_RUN_STORE_PATH"] == str(
        tmp_path / "explicit-runs.sqlite"
    )
    assert environment["WORK_HARNESS_PROVIDER_STORE_PATH"] == str(
        root / "data" / "providers.sqlite"
    )
    assert environment["WORK_HARNESS_SETTINGS_PATH"] == str(
        root / "data" / "settings.json"
    )
    assert environment["WORK_HARNESS_EVALUATION_OUTPUT"] == str(
        root / "reports" / "latest-evaluation.json"
    )
    assert environment["WORK_HARNESS_SCHEMA_OUTPUT"] == str(root / "exports" / "schemas")


def test_frozen_project_root_uses_bundled_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "runtime.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert default_project_root() == tmp_path.resolve()
