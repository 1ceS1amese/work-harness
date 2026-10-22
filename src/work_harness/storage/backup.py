"""Offline backup bundles: business state and the assets needed to resume it."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp

from work_harness import __version__
from work_harness.storage.blackboard_lock import BlackboardProcessLock

DATABASES = ("checkpoints.sqlite", "runs.sqlite", "providers.sqlite", "blackboard.sqlite")
ASSETS = ("config", "prompts", "skills")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader,
        closing(sqlite3.connect(destination)) as writer,
    ):
        reader.backup(writer)
        if writer.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError(f"database integrity check failed: {source.name}")
    destination.chmod(0o600)


def _copy(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"backup assets must be regular files: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(source, destination)
    destination.chmod(0o600)


def _copy_assets(project_root: Path, destination: Path, *, deployment_root: Path | None) -> None:
    for name in ASSETS:
        if not (project_root / name).is_dir():
            raise ValueError(f"missing runtime assets: {name}")
        (destination / name).mkdir(mode=0o700)
        for path in sorted((project_root / name).rglob("*")):
            if path.is_symlink():
                raise ValueError(f"backup assets must not contain symbolic links: {path}")
            if path.is_file():
                _copy(path, destination / path.relative_to(project_root))
    # These deployment files can contain secrets; the entire bundle is private.
    deployment_root = deployment_root or project_root
    for name in (
        ".env",
        ".env.distributed",
        "deploy/runtime.env",
        "deploy/runtime.distributed.env",
    ):
        if (deployment_root / name).is_file():
            _copy(deployment_root / name, destination / name)


def _write_manifest(destination: Path, *, backend: str, databases: list[str]) -> None:
    manifest = {
        "format_version": 1,
        "backend": backend,
        "application_version": __version__,
        "profile": os.getenv("WORK_HARNESS_PROFILE", "setup"),
        "created_at": datetime.now(UTC).isoformat(),
        "databases": databases,
        "files": [
            {
                "name": p.relative_to(destination).as_posix(),
                "size": p.stat().st_size,
                "sha256": file_sha256(p),
            }
            for p in sorted(destination.rglob("*"))
            if p.is_file()
        ],
        "restore_requires": "Original app version, profile and environment overrides",
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o600)


def create_postgres_backup(
    sql_dump: Path,
    destination: Path,
    *,
    project_root: Path,
    local_data: Path,
    deployment_root: Path | None = None,
) -> Path:
    """Package a successful pg_dump after API/Workers have stopped.

    pg_dump owns the database snapshot. This function adds the matching runtime
    assets and optional local settings; it never connects to or overwrites PostgreSQL.
    """
    sql_dump, destination = sql_dump.resolve(), destination.resolve()
    project_root, local_data = project_root.resolve(), local_data.resolve()
    if destination.exists():
        raise FileExistsError(destination)
    if destination.is_relative_to(local_data) or any(
        destination.is_relative_to(project_root / name) for name in ASSETS
    ):
        raise ValueError("backup destination must be outside the data and asset directories")
    if not sql_dump.is_file() or not sql_dump.stat().st_size:
        raise ValueError("a successful nonempty PostgreSQL dump is required")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        _copy(sql_dump, staging / "database.sql")
        databases = []
        if (local_data / "providers.sqlite").is_file():
            if not (local_data / "providers.sqlite.key").is_file():
                raise ValueError("provider database requires its encryption key")
            (staging / "var").mkdir(mode=0o700)
            backup_database(local_data / "providers.sqlite", staging / "var/providers.sqlite")
            _copy(local_data / "providers.sqlite.key", staging / "var/providers.sqlite.key")
            databases.append("providers.sqlite")
        if (local_data / "settings.json").is_file():
            _copy(local_data / "settings.json", staging / "var/settings.json")
        _copy_assets(project_root, staging, deployment_root=deployment_root)
        _write_manifest(staging, backend="postgres", databases=databases)
        if destination.exists():
            raise FileExistsError(destination)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def create_sqlite_backup(
    source: Path, destination: Path, *, project_root: Path, deployment_root: Path | None = None
) -> Path:
    """Require a stopped application; publish only a complete bundle, never overwrite."""
    source, destination, project_root = (
        source.resolve(),
        destination.resolve(),
        project_root.resolve(),
    )
    if destination.exists():
        raise FileExistsError(destination)
    if destination.is_relative_to(source) or any(
        destination.is_relative_to(project_root / name) for name in ASSETS
    ):
        raise ValueError("backup destination must be outside the data and asset directories")
    required = (*DATABASES[:3], "providers.sqlite.key")
    for name in required:
        if not (source / name).is_file():
            raise ValueError(f"missing required backup source: {name}")
    for name in ASSETS:
        if not (project_root / name).is_dir():
            raise ValueError(f"missing runtime assets: {name}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ExitStack() as stack:
        for name in DATABASES:
            lock = BlackboardProcessLock(source / name, reentrant=False)
            lock.acquire()
            stack.callback(lock.release)
        if not (source / "blackboard.sqlite").exists():
            with closing(
                sqlite3.connect((source / "runs.sqlite").as_uri() + "?mode=ro", uri=True)
            ) as db:
                executions = db.execute(
                    "SELECT execution FROM run_index WHERE execution IS NOT NULL"
                )
                if any(json.loads(row[0]).get("engine") == "blackboard" for row in executions):
                    raise ValueError("blackboard Run exists but blackboard.sqlite is missing")
        staging = Path(mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
        try:
            (staging / "var").mkdir(mode=0o700)
            present = [name for name in DATABASES if (source / name).is_file()]
            for name in present:
                backup_database(source / name, staging / "var" / name)
            for name in ("providers.sqlite.key", "settings.json"):
                if (source / name).is_file():
                    _copy(source / name, staging / "var" / name)
            _copy_assets(project_root, staging, deployment_root=deployment_root)
            _write_manifest(staging, backend="sqlite", databases=present)
            if destination.exists():
                raise FileExistsError(destination)
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return destination


def restore_backup(bundle: Path, destination: Path) -> Path:
    """Verify and restore into a new isolated project directory; never replace live data."""
    bundle, destination = bundle.resolve(), destination.resolve()
    if destination.exists():
        raise FileExistsError(destination)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1 or manifest.get("backend") not in {"sqlite", "postgres"}:
        raise ValueError("unsupported backup format")
    files = {}
    for entry in manifest["files"]:
        name = entry["name"]
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or name in files:
            raise ValueError("invalid backup member")
        source = bundle / name
        if source.is_symlink() or not source.resolve().is_relative_to(bundle):
            raise ValueError("backup member escapes bundle")
        if (
            not source.is_file()
            or source.stat().st_size != entry["size"]
            or file_sha256(source) != entry["sha256"]
        ):
            raise ValueError(f"backup member missing or damaged: {name}")
        files[name] = source
    databases = manifest["databases"]
    if not isinstance(databases, list) or not set(databases) <= set(DATABASES):
        raise ValueError("invalid database list")
    required = (
        {f"var/{name}" for name in (*DATABASES[:3], "providers.sqlite.key")}
        if manifest["backend"] == "sqlite"
        else {"database.sql"}
    )
    if "providers.sqlite" in databases:
        required.add("var/providers.sqlite.key")
    required.update(f"var/{name}" for name in databases)
    if not required <= files.keys() or any(
        not any(name.startswith(asset + "/") for name in files) for asset in ASSETS
    ):
        raise ValueError("backup is incomplete")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        for name, source in files.items():
            _copy(source, staging / name)
        for name in databases:
            with closing(sqlite3.connect(staging / "var" / name)) as db:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError(f"database integrity check failed: {name}")
        if destination.exists():
            raise FileExistsError(destination)
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def restore_sqlite_backup(bundle: Path, destination: Path) -> Path:
    return restore_backup(bundle, destination)
