from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    version: str
    body: str


class PromptRegistry:
    def __init__(self, prompt_dir: Path) -> None:
        self._prompt_dir = prompt_dir
        self._cache: dict[str, PromptTemplate] = {}

    def get(self, name: str) -> PromptTemplate:
        if name in self._cache:
            return self._cache[name]
        path = self._prompt_dir / f"{name}.md"
        if not path.is_file():
            raise KeyError(f"prompt not found: {name}")
        raw = path.read_text(encoding="utf-8")
        version = "unversioned"
        body = raw
        if raw.startswith("---\n"):
            _, header, body = raw.split("---", 2)
            for line in header.splitlines():
                key, separator, value = line.partition(":")
                if separator and key.strip() == "version":
                    version = value.strip()
        template = PromptTemplate(name=name, version=version, body=body.strip())
        self._cache[name] = template
        return template

