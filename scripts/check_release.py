#!/usr/bin/env python3
"""Validate repository metadata and Best-README-Template documentation structure."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

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
        "About the Project",
        "Getting Started",
        "Usage",
        "Roadmap",
        "Contributing",
        "License",
        "Contact",
        "Acknowledgments",
    ),
    "README.zh-CN.md": (
        "项目介绍",
        "快速开始",
        "使用方式",
        "项目进度与后续方向",
        "参与贡献",
        "许可证",
        "联系与反馈",
        "致谢",
    ),
}

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
VERSION_ASSIGNMENT = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def fail(message: str, errors: list[str]) -> None:
    errors.append(message)


def validate_readme(root: Path, name: str, errors: list[str]) -> None:
    path = root / name
    text = path.read_text(encoding="utf-8")
    prose = re.sub(r"^```[^\n]*\n.*?^```\s*$", "", text, flags=re.MULTILINE | re.DOTALL)
    lines = prose.splitlines()

    if not re.search(r"<h1>\s*Work Harness\s*</h1>\s*<p>\s*[^<\s][^<]*</p>", prose):
        fail(f"{name}: project title and short introduction are required", errors)
    toc_title = "Table of Contents" if name == "README.md" else "目录"
    if not re.search(
        rf"<details>\s*<summary>{toc_title}</summary>.*?<ol>.*?</ol>\s*</details>",
        prose,
        re.DOTALL,
    ):
        fail(f"{name}: collapsible table of contents is required", errors)
    if '<a id="readme-top"></a>' not in prose or 'href="#readme-top"' not in prose:
        fail(f"{name}: back-to-top navigation is required", errors)

    headings = [line[3:].strip() for line in lines if line.startswith("## ")]
    required = README_CONTRACTS[name]
    positions: list[int] = []
    for heading in required:
        if heading not in headings:
            fail(f"{name}: missing required section {heading!r}", errors)
            continue
        positions.append(headings.index(heading))
    if positions != sorted(positions):
        fail(f"{name}: required sections are not in Best-README-Template order", errors)

    anchors = set(re.findall(r'<a\s+id="([^"]+)"', prose))
    anchors.update(
        re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        for heading in re.findall(r"^#{1,6}\s+(.+)$", prose, re.MULTILINE)
    )
    references = dict(re.findall(r"^\[([^\]]+)\]:\s*(\S+)\s*$", prose, re.MULTILINE))
    for reference in re.findall(r"\]\[([^\]]+)\]", prose):
        if reference not in references:
            fail(f"{name}: undefined reference link: {reference}", errors)
    targets = (
        [match.group(1) for match in MARKDOWN_LINK.finditer(prose)]
        + re.findall(r'href="([^"]+)"', prose)
        + list(references.values())
    )
    for target in targets:
        target = target.strip().split(maxsplit=1)[0].strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        if parsed.path and not (path.parent / unquote(parsed.path)).exists():
            fail(f"{name}: local link target does not exist: {parsed.path}", errors)
        elif not parsed.path and parsed.fragment and unquote(parsed.fragment) not in anchors:
            fail(f"{name}: local anchor does not exist: {parsed.fragment}", errors)


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
            validate_readme(root, name, errors)

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
