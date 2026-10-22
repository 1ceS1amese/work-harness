from __future__ import annotations

import os
import sys
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    root: Path
    data: Path
    logs: Path
    reports: Path
    exports: Path


def frozen_bundle_root() -> Path | None:
    """Return PyInstaller's extraction directory when running a frozen build."""

    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        return Path(bundle_root).resolve()
    return None


def user_data_root(
    environment: Mapping[str, str] | None = None,
    *,
    platform_name: str | None = None,
    home: Path | None = None,
) -> Path:
    values = os.environ if environment is None else environment
    override = values.get("WORK_HARNESS_DATA_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    platform = platform_name or sys.platform
    user_home = (home or Path.home()).resolve()
    if platform == "win32":
        local_app_data = values.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else user_home / "AppData" / "Local"
        return (base / "WorkHarness").resolve()
    if platform == "darwin":
        return (user_home / "Library" / "Application Support" / "WorkHarness").resolve()

    xdg_data_home = values.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data_home) if xdg_data_home else user_home / ".local" / "share"
    return (base / "work-harness").resolve()


def prepare_application_environment(
    environment: MutableMapping[str, str] | None = None,
    *,
    platform_name: str | None = None,
    home: Path | None = None,
) -> ApplicationPaths:
    """Create writable application directories and configure persistent stores."""

    values = os.environ if environment is None else environment
    root = user_data_root(values, platform_name=platform_name, home=home)
    paths = ApplicationPaths(
        root=root,
        data=root / "data",
        logs=root / "logs",
        reports=root / "reports",
        exports=root / "exports",
    )
    for directory in (paths.data, paths.logs, paths.reports, paths.exports):
        directory.mkdir(parents=True, exist_ok=True)
    # Packaged applications must never silently run the deterministic Mock Provider.
    # Developers can still opt in with WORK_HARNESS_PROFILE=local.
    values.setdefault("WORK_HARNESS_PROFILE", "setup")
    values.setdefault(
        "WORK_HARNESS_CHECKPOINT_PATH",
        str(paths.data / "checkpoints.sqlite"),
    )
    values.setdefault("WORK_HARNESS_RUN_STORE_PATH", str(paths.data / "runs.sqlite"))
    values.setdefault("WORK_HARNESS_BLACKBOARD_PATH", str(paths.data / "blackboard.sqlite"))
    values.setdefault(
        "WORK_HARNESS_PROVIDER_STORE_PATH",
        str(paths.data / "providers.sqlite"),
    )
    values.setdefault(
        "WORK_HARNESS_SETTINGS_PATH",
        str(paths.data / "settings.json"),
    )
    values.setdefault(
        "WORK_HARNESS_EVALUATION_OUTPUT",
        str(paths.reports / "latest-evaluation.json"),
    )
    values.setdefault(
        "WORK_HARNESS_SCHEMA_OUTPUT",
        str(paths.exports / "schemas"),
    )
    return paths
