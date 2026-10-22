from __future__ import annotations

import argparse
from pathlib import Path

from work_harness.storage.backup import create_postgres_backup, restore_backup


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Package an offline PostgreSQL dump and runtime assets"
    )
    parser.add_argument("source", type=Path, help="successful SQL dump, or bundle with --restore")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--local-data", type=Path, default=Path("var"))
    parser.add_argument("--deployment-root", type=Path)
    parser.add_argument(
        "--restore", action="store_true", help="verify/extract; does NOT import SQL"
    )
    args = parser.parse_args()
    if args.restore:
        result = restore_backup(args.source, args.destination)
    else:
        result = create_postgres_backup(
            args.source,
            args.destination,
            project_root=args.project_root,
            local_data=args.local_data,
            deployment_root=args.deployment_root,
        )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
