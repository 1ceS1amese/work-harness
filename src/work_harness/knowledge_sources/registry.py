"""Source registration and discovery independent of graph topology."""

from __future__ import annotations

from datetime import datetime

from work_harness.blackboard.activation import candidate_for_view, make_view
from work_harness.blackboard.models import Intent, digest
from work_harness.blackboard.protocol import BlackboardError, BoardSnapshot
from work_harness.knowledge_sources.base import (
    ActivationCandidate,
    BoardChangeSet,
    BoardView,
    KnowledgeSource,
)


class KnowledgeSourceRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, KnowledgeSource] = {}
        self._frozen_specs: dict[str, str] | None = None

    def register(self, source: KnowledgeSource) -> None:
        if self._frozen_specs is not None:
            raise BlackboardError("source registry is frozen for this compiled graph")
        if source.spec.id in self._sources:
            raise BlackboardError(f"knowledge source already registered: {source.spec.id}")
        if source.spec.id in {
            "bootstrap",
            "assess",
            "commit",
            "finish",
            "wait",
            "__start__",
            "__end__",
        }:
            raise BlackboardError("knowledge source id conflicts with a control node")
        self._sources[source.spec.id] = source

    def freeze(self) -> None:
        if self._frozen_specs is None:
            self._frozen_specs = {key: digest(source.spec) for key, source in self._sources.items()}

    def get(self, source_id: str) -> KnowledgeSource:
        try:
            source = self._sources[source_id]
        except KeyError as exc:
            raise BlackboardError(f"unknown knowledge source: {source_id}") from exc
        if self._frozen_specs is not None and digest(source.spec) != self._frozen_specs[source_id]:
            raise BlackboardError("knowledge source configuration changed during a run")
        return source

    def sources(self) -> tuple[KnowledgeSource, ...]:
        return tuple(self.get(key) for key in sorted(self._sources))

    def discover(
        self,
        snapshot: BoardSnapshot,
        changes: BoardChangeSet,
        now: datetime,
    ) -> list[tuple[ActivationCandidate, BoardView]]:
        candidates: list[tuple[ActivationCandidate, BoardView]] = []
        for intent in snapshot.objects:
            if not isinstance(intent, Intent) or intent.status not in {"open", "blocked"}:
                continue
            for source in self.sources():
                view = make_view(snapshot, intent, source.spec, now)
                match = source.match(view.model_copy(deep=True), changes)
                if match is None:
                    continue
                expected = candidate_for_view(source.spec, view)
                if (
                    match.source_id != source.spec.id
                    or match.source_version != source.spec.version
                    or match.board_id != snapshot.board_id
                    or match.intent_ref != intent.ref
                    or match.input_refs != expected.input_refs
                    or match.input_fingerprint != expected.input_fingerprint
                    or match.id != expected.id
                    or match.intent_priority != expected.intent_priority
                    or match.conflict != expected.conflict
                ):
                    raise BlackboardError("knowledge source returned an invalid activation match")
                candidates.append((match, view))
        return candidates
