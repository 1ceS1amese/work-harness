from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DATABASES = ("checkpoints.sqlite", "runs.sqlite", "providers.sqlite")
PROVIDER_KEY = "providers.sqlite.key"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create consistent Work Harness SQLite backups")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()

    source_dir = args.source.resolve()
    destination_dir = args.destination.resolve()
    missing = [name for name in DATABASES if not (source_dir / name).is_file()]
    if missing:
        raise SystemExit(f"missing Work Harness databases in {source_dir}: {', '.join(missing)}")
    destination_dir.mkdir(parents=True, exist_ok=False)

    files: list[dict[str, object]] = []
    for name in DATABASES:
        source = source_dir / name
        destination = destination_dir / name
        backup_database(source, destination)
        files.append(
            {
                "name": name,
                "size": destination.stat().st_size,
                "sha256": file_sha256(destination),
            }
        )

    provider_key = source_dir / PROVIDER_KEY
    if provider_key.is_file():
        key_destination = destination_dir / PROVIDER_KEY
        shutil.copyfile(provider_key, key_destination)
        key_destination.chmod(0o600)
        files.append(
            {
                "name": PROVIDER_KEY,
                "size": key_destination.stat().st_size,
                "sha256": file_sha256(key_destination),
            }
        )

    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "files": files,
    }
    (destination_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(destination_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
