#!/usr/bin/env python3
"""Verify that a Work Harness wheel is complete and usable outside the source tree."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REQUIRED_RUNTIME_FILES = (
    "work_harness/_runtime/config/runtime.yaml",
    "work_harness/_runtime/config/models.yaml",
    "work_harness/_runtime/prompts/planner.md",
    "work_harness/_runtime/schemas/model_request.schema.json",
    "work_harness/_runtime/skills/repository-analysis/SKILL.md",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()

    if not wheel.is_file() or wheel.suffix != ".whl":
        parser.error(f"wheel does not exist: {wheel}")

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = [name for name in REQUIRED_RUNTIME_FILES if name not in names]
        if missing:
            raise SystemExit(f"wheel is missing runtime files: {', '.join(missing)}")

        metadata_name = next(
            (name for name in names if name.endswith(".dist-info/METADATA")),
            None,
        )
        if metadata_name is None:
            raise SystemExit("wheel does not contain package metadata")
        metadata = archive.read(metadata_name).decode("utf-8")
        if "License-Expression: MIT" not in metadata or "License-File: LICENSE" not in metadata:
            raise SystemExit("wheel metadata does not include the MIT license declaration")

        with tempfile.TemporaryDirectory(prefix="work-harness-wheel-") as temp_dir:
            archive.extractall(temp_dir)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = temp_dir
            command = [sys.executable, "-m", "work_harness", "check-config"]
            completed = subprocess.run(
                command,
                cwd=temp_dir,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode:
                raise SystemExit(
                    "packaged CLI configuration check failed:\n"
                    f"{completed.stdout}{completed.stderr}"
                )

    print(f"Verified wheel runtime assets and CLI: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
