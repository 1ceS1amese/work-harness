from __future__ import annotations

import argparse
from pathlib import Path

from work_harness.storage.backup import create_sqlite_backup, restore_sqlite_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up a stopped Work Harness SQLite deployment")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--restore", action="store_true", help="restore a bundle into a NEW directory"
    )
    parser.add_argument("--deployment-root", type=Path)
    args = parser.parse_args()
    if args.restore:
        result = restore_sqlite_backup(args.source, args.destination)
    else:
        result = create_sqlite_backup(
            args.source,
            args.destination,
            project_root=args.project_root,
            deployment_root=args.deployment_root,
        )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
