#!/usr/bin/env python3
"""Validate repository metadata and Standard Readme release invariants."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

REQUIRED_FILES = (
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "README.zh-CN.md",
    "SECURITY.md",
    ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/ISSUE_TEMPLATE/feature.yml",
    ".github/ISSUE_TEMPLATE/question.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/pull_request_template.md",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".github/workflows/windows-exe.yml",
    ".github/dependabot.yml",
    "docs/windows-executable.md",
    "packaging/work-harness.spec",
    "packaging/windows-version-info.txt",
    "scripts/build_windows.ps1",
)

README_CONTRACTS = {
    "README.md": (
        "Table of Contents",
        "Security",
        "Background",
        "Install",
        "Usage",
        "Contributing",
        "License",
    ),
    "README.zh-CN.md": (
        "目录",
        "安全",
        "背景",
        "安装",
        "使用",
        "贡献",
        "许可证",
    ),
}

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
VERSION_ASSIGNMENT = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def fail(message: str, errors: list[str]) -> None:
    errors.append(message)


def validate_readme(root: Path, name: str, description: str, errors: list[str]) -> None:
    path = root / name
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    if description not in lines:
        fail(f"{name}: short description must exactly match pyproject.toml", errors)

    headings = [line[3:].strip() for line in lines if line.startswith("## ")]
    required = README_CONTRACTS[name]
    positions: list[int] = []
    for heading in required:
        if heading not in headings:
            fail(f"{name}: missing required section {heading!r}", errors)
            continue
        positions.append(headings.index(heading))
    if positions != sorted(positions):
        fail(f"{name}: required sections are not in Standard Readme order", errors)
    if not headings or headings[-1] != required[-1]:
        fail(f"{name}: license section must be the final level-two section", errors)

    for match in MARKDOWN_LINK.finditer(text):
        target = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        relative_path = target.split("#", maxsplit=1)[0]
        if relative_path and not (path.parent / relative_path).exists():
            fail(f"{name}: local link target does not exist: {relative_path}", errors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tag", help="require a release tag matching v<project.version>")
    args = parser.parse_args()
    root = args.root.resolve()
    errors: list[str] = []

    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            fail(f"missing release file: {relative}", errors)

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    package_init = (root / "src/work_harness/__init__.py").read_text(encoding="utf-8")
    version_match = VERSION_ASSIGNMENT.search(package_init)
    package_version = version_match.group(1) if version_match else None
    if package_version != project["version"]:
        fail(
            "pyproject.toml project.version must match work_harness.__version__ "
            f"({project['version']!r} != {package_version!r})",
            errors,
        )
    if args.tag and args.tag != f"v{project['version']}":
        fail(
            f"release tag must be v{project['version']}, received {args.tag!r}",
            errors,
        )
    if project.get("license") != "MIT" or "LICENSE" not in project.get("license-files", []):
        fail("pyproject.toml must publish the MIT license expression and LICENSE file", errors)

    frontend_package = json.loads(
        (root / "frontend/package.json").read_text(encoding="utf-8")
    )
    if frontend_package.get("version") != project["version"]:
        fail(
            "frontend/package.json version must match pyproject.toml project.version "
            f"({frontend_package.get('version')!r} != {project['version']!r})",
            errors,
        )

    for name in README_CONTRACTS:
        if (root / name).is_file():
            validate_readme(root, name, project["description"], errors)

    if errors:
        print("Release checks failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(
        f"Release checks passed for {project['name']} {project['version']} "
        f"({len(REQUIRED_FILES)} community and automation files)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
