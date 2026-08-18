from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from work_harness.tools.base import ToolPermissionError


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=1_000)


class EchoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class EchoReadTool:
    name = "echo_read"
    input_model = EchoInput
    output_model = EchoOutput

    async def execute(self, payload: BaseModel) -> BaseModel:
        validated = EchoInput.model_validate(payload)
        return EchoOutput(text=validated.text)


class WorkspaceReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["read", "list", "search"]
    path: str = Field(default=".", min_length=1, max_length=500)
    query: str | None = Field(default=None, min_length=1, max_length=200)
    start_line: int = Field(default=1, ge=1, le=1_000_000)
    max_lines: int = Field(default=120, ge=1, le=200)
    max_results: int = Field(default=30, ge=1, le=100)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> WorkspaceReadInput:
        if self.operation == "search" and not self.query:
            raise ValueError("search operation requires query")
        return self


class WorkspaceSearchMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    line: int = Field(ge=1)
    text: str


class WorkspaceReadOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["read", "list", "search"]
    path: str
    content: str = ""
    entries: list[str] = Field(default_factory=list)
    matches: list[WorkspaceSearchMatch] = Field(default_factory=list)
    truncated: bool = False


class WorkspaceReadTool:
    """Bounded text inspection rooted at one configured workspace directory."""

    name = "workspace_read"
    input_model = WorkspaceReadInput
    output_model = WorkspaceReadOutput

    _blocked_directories = frozenset({".git", ".venv", "node_modules", "var", "__pycache__"})
    _blocked_names = frozenset(
        {
            ".env",
            ".npmrc",
            ".pypirc",
            "credentials.json",
            "id_rsa",
            "id_ed25519",
            "secrets.json",
            "secrets.yaml",
            "secrets.yml",
        }
    )
    _blocked_suffixes = frozenset({".key", ".pem", ".p12", ".pfx"})
    _max_file_bytes = 256_000
    _max_scanned_files = 500

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    async def execute(self, payload: BaseModel) -> BaseModel:
        import asyncio

        request = WorkspaceReadInput.model_validate(payload)
        return await asyncio.to_thread(self._execute_sync, request)

    def _execute_sync(self, request: WorkspaceReadInput) -> WorkspaceReadOutput:
        target = self._resolve(request.path)
        if request.operation == "read":
            return self._read(target, request)
        if request.operation == "list":
            return self._list(target, request)
        return self._search(target, request)

    def _resolve(self, raw_path: str) -> Path:
        if "\x00" in raw_path:
            raise ToolPermissionError("workspace path contains an invalid character")
        candidate = (self.root / raw_path).resolve()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise ToolPermissionError("workspace path escapes the configured root") from exc
        self._assert_allowed(relative)
        return candidate

    def _assert_allowed(self, relative: Path) -> None:
        for part in relative.parts:
            lowered = part.casefold()
            if lowered.startswith(".env") and lowered != ".env.example":
                raise ToolPermissionError("workspace environment files are excluded")
            if lowered in self._blocked_directories or lowered in self._blocked_names:
                raise ToolPermissionError("workspace path is excluded by read policy")
            if Path(lowered).suffix in self._blocked_suffixes:
                raise ToolPermissionError("workspace file type is excluded by read policy")

    def _relative(self, path: Path) -> str:
        relative = path.relative_to(self.root)
        return relative.as_posix() or "."

    def _read(self, target: Path, request: WorkspaceReadInput) -> WorkspaceReadOutput:
        if not target.is_file():
            raise FileNotFoundError(f"workspace file does not exist: {request.path}")
        if target.stat().st_size > self._max_file_bytes:
            raise ValueError("workspace file exceeds the read size limit")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError("workspace reader only supports UTF-8 text files") from exc
        start = request.start_line - 1
        selected = lines[start : start + request.max_lines]
        numbered = [f"{index}: {line}" for index, line in enumerate(selected, request.start_line)]
        return WorkspaceReadOutput(
            operation="read",
            path=self._relative(target),
            content="\n".join(numbered),
            truncated=start + len(selected) < len(lines),
        )

    def _list(self, target: Path, request: WorkspaceReadInput) -> WorkspaceReadOutput:
        if not target.is_dir():
            raise NotADirectoryError(f"workspace directory does not exist: {request.path}")
        entries: list[str] = []
        truncated = False
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name)):
            try:
                relative = child.resolve().relative_to(self.root)
                self._assert_allowed(relative)
            except (ValueError, ToolPermissionError):
                continue
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{self._relative(child.resolve())}{suffix}")
            if len(entries) >= request.max_results:
                truncated = True
                break
        return WorkspaceReadOutput(
            operation="list",
            path=self._relative(target),
            entries=entries,
            truncated=truncated,
        )

    def _search(self, target: Path, request: WorkspaceReadInput) -> WorkspaceReadOutput:
        if not target.exists():
            raise FileNotFoundError(f"workspace path does not exist: {request.path}")
        query = (request.query or "").casefold()
        matches: list[WorkspaceSearchMatch] = []
        truncated = False
        scanned = 0
        for file_path in self._iter_text_candidates(target):
            scanned += 1
            if scanned > self._max_scanned_files:
                truncated = True
                break
            try:
                if file_path.stat().st_size > self._max_file_bytes:
                    continue
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                if query not in line.casefold():
                    continue
                matches.append(
                    WorkspaceSearchMatch(
                        path=self._relative(file_path),
                        line=line_number,
                        text=line[:500],
                    )
                )
                if len(matches) >= request.max_results:
                    truncated = True
                    break
            if truncated and len(matches) >= request.max_results:
                break
        return WorkspaceReadOutput(
            operation="search",
            path=self._relative(target),
            matches=matches,
            truncated=truncated,
        )

    def _iter_text_candidates(self, target: Path):
        if target.is_file():
            yield target
            return
        for root, directories, filenames in os.walk(target):
            root_path = Path(root)
            directories[:] = [
                name
                for name in sorted(directories)
                if name.casefold() not in self._blocked_directories
                and not (root_path / name).is_symlink()
            ]
            for filename in sorted(filenames):
                path = root_path / filename
                try:
                    relative = path.resolve().relative_to(self.root)
                    self._assert_allowed(relative)
                except (ValueError, ToolPermissionError):
                    continue
                if path.is_symlink():
                    continue
                yield path
