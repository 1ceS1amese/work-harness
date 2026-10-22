# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

PROJECT_ROOT = Path(SPECPATH).resolve().parent


def data_tree(source: str, destination: str):
    root = PROJECT_ROOT / source
    result = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(":Zone.Identifier"):
            continue
        if source == "evaluation" and path.relative_to(root).parts[0] == "reports":
            continue
        relative_parent = path.relative_to(root).parent
        target = Path(destination) / relative_parent
        result.append((str(path), str(target)))
    return result


def distribution_metadata(name: str):
    try:
        return copy_metadata(name)
    except PackageNotFoundError:
        return []


datas = []
for source, destination in (
    ("config", "config"),
    ("prompts", "prompts"),
    ("schemas", "schemas"),
    ("skills", "skills"),
    ("evaluation", "evaluation"),
    ("frontend/dist", "frontend/dist"),
):
    datas += data_tree(source, destination)

for distribution in (
    "work-harness",
    "langgraph",
    "langgraph-checkpoint",
    "langgraph-checkpoint-sqlite",
):
    datas += distribution_metadata(distribution)

hiddenimports = sorted(
    set(
        collect_submodules("langgraph.checkpoint.sqlite")
        + [
            "aiosqlite",
            "work_harness.llm.openai_compatible",
            "work_harness.storage.provider_sqlite",
            "work_harness.storage.sqlite",
        ]
    )
)

analysis = Analysis(
    [str(PROJECT_ROOT / "src/work_harness/desktop.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="work-harness",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=(
        str(PROJECT_ROOT / "packaging/windows-version-info.txt")
        if sys.platform == "win32"
        else None
    ),
)
