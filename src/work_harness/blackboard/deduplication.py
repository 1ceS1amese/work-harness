"""Exact knowledge deduplication; source lineage is kept in execution records."""

from __future__ import annotations

from typing import Any

from work_harness.blackboard.models import (
    BoardObject,
    Fact,
    Hint,
    Intent,
    ObjectRef,
    content_of,
    digest,
)


def knowledge_digest(item: BoardObject) -> str:
    content = content_of(item).model_dump(mode="json")
    for field in ("derived_from", "source_refs", "confidence"):
        content.pop(field, None)
    if isinstance(item, Fact):
        content["confirmation"] = item.verification.model_dump(
            mode="json",
            include={"verifier_id", "verifier_version", "method", "checks"},
        )
    return digest(content)


def _reusable(item: BoardObject) -> bool:
    if isinstance(item, Fact):
        return item.lifecycle == "valid"
    if isinstance(item, Hint):
        return item.status == "active"
    return isinstance(item, Intent) and item.status in {"open", "blocked", "satisfied"}


def _remap(value: Any, aliases: dict[str, ObjectRef]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"object_id", "revision"} and value["object_id"] in aliases:
            return aliases[value["object_id"]].model_dump()
        return {key: _remap(item, aliases) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_remap(item, aliases) for item in value]
    return value


def deduplicate(
    objects: dict[str, BoardObject],
    new_ids: set[str],
    local_refs: dict[str, ObjectRef],
) -> set[str]:
    new_ids = set(new_ids)
    aliases: dict[str, ObjectRef] = {}
    while True:
        known = {
            knowledge_digest(item): item.ref
            for item in objects.values()
            if item.id not in new_ids and _reusable(item)
        }
        removed: set[str] = set()
        for item_id in sorted(new_ids):
            item = objects[item_id]
            signature = knowledge_digest(item)
            if signature in known:
                aliases[item_id] = known[signature]
                removed.add(item_id)
            else:
                known[signature] = item.ref
        if not removed:
            break
        for item_id in removed:
            del objects[item_id]
        new_ids -= removed
        for item_id in new_ids:
            item = objects[item_id]
            data = item.model_dump()
            for field in (
                "derived_from",
                "source_refs",
                "links",
                "root_intent_ref",
                "parent_intent_ref",
            ):
                if field in data:
                    data[field] = _remap(data[field], aliases)
            updated = type(item).model_validate(data)
            objects[item_id] = updated.model_copy(
                update={"content_digest": digest(content_of(updated))}
            )
        for key, ref in local_refs.items():
            while ref.object_id in aliases:
                ref = aliases[ref.object_id]
            local_refs[key] = ref
    return new_ids
