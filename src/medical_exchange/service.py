from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from .exceptions import ImportFormatError
from .models import EntityKey, IdentityDecision, PendingReference, Source, Version
from .store import parse_canonical_id

if TYPE_CHECKING:
    from .store import RecordStore

UNIFIED_FORMAT = "unified.records.v1"


def export_bundle(store: "RecordStore") -> dict[str, Any]:
    """Export current data, retained versions, relationships and identity work."""
    entities = []
    relationships = []

    for key in sorted(store.entities, key=lambda item: item.canonical_id()):
        entity = store.entities[key]
        source = store.sources.get(key.source_id)
        entities.append(
            {
                "id": key.canonical_id(),
                "type": key.entity_type,
                "source": {"id": key.source_id, "name": source.name if source else ""},
                "source_record_id": key.source_record_id,
                "current_version": entity.current_version,
                "data": entity.current.data,
                "versions": [
                    {
                        "version": item.version,
                        "content_hash": item.content_hash,
                        "data": item.data
                        if item.version == entity.current_version
                        else None,
                    }
                    for _, item in sorted(entity.versions.items())
                ],
            }
        )

        for field_name, target_type in (
            ("patient_ref", "patient"),
            ("encounter_ref", "encounter"),
        ):
            if field_name not in entity.current.data:
                continue
            target_key = EntityKey(
                key.source_id, target_type, entity.current.data[field_name]
            )
            relationships.append(
                {
                    "source_entity": key.canonical_id(),
                    "field": field_name,
                    "target_type": target_type,
                    "target_entity": target_key.canonical_id(),
                    "status": "resolved" if target_key in store.entities else "pending",
                }
            )

    relationships.sort(
        key=lambda item: (item["source_entity"], item["field"], item["target_entity"])
    )

    groups = []
    for group_id, members in sorted(store.active_patient_groups().items()):
        merge_decision = next(
            item
            for item in store.decisions
            if item.group_id == group_id and item.action == "merge"
        )
        groups.append(
            {
                "id": group_id,
                "members": members,
                "merge_decision_id": merge_decision.decision_id,
                "basis": _decision_as_dict(merge_decision),
            }
        )

    return {
        "format": UNIFIED_FORMAT,
        "sources": [
            {"id": source.id, "name": source.name}
            for source in sorted(store.sources.values(), key=lambda item: item.id)
        ],
        "entities": entities,
        "relationships": relationships,
        "pending_relationships": [
            {
                "source_entity": ref.source_entity,
                "field": ref.field_name,
                "target_type": ref.target_type,
                "target_entity": ref.target.canonical_id(),
            }
            for ref in sorted(
                store.pending,
                key=lambda item: (item.source_entity, item.field_name),
            )
        ],
        "patient_groups": groups,
        "identity_decisions": [
            _decision_as_dict(decision) for decision in store.decisions
        ],
    }


def _decision_as_dict(decision: IdentityDecision) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "action": decision.action,
        "group_id": decision.group_id,
        "members": list(decision.members),
        "reason": decision.reason,
        "operator": decision.operator,
        "decided_at": decision.decided_at,
        "supersedes": decision.supersedes,
        "evidence": list(decision.evidence),
    }


def import_unified_bundle(bundle: dict[str, Any]) -> "RecordStore":
    """Rebuild a fresh store from an export, preserving all manual identity work.

    Only the current version carries data; older versions are reconstructed as
    hash-only history. Manual decisions and their evidence are replayed exactly.
    """
    from .models import Entity
    from .store import RecordStore

    if not isinstance(bundle, dict) or bundle.get("format") != UNIFIED_FORMAT:
        raise ImportFormatError(f"unsupported format; expected {UNIFIED_FORMAT!r}")

    store = RecordStore()
    for raw_source in bundle.get("sources", []):
        store.sources[raw_source["id"]] = Source(
            id=raw_source["id"], name=raw_source.get("name", "")
        )

    for raw_entity in bundle.get("entities", []):
        key = parse_canonical_id(raw_entity["id"])
        versions: dict[int, Version] = {}
        current_version = raw_entity["current_version"]
        current_data = None
        for item in raw_entity.get("versions", []):
            data = item.get("data")
            if item["version"] == current_version:
                if data is None:
                    raise ImportFormatError(
                        f"current version data missing for {raw_entity['id']}"
                    )
                current_data = data
            versions[item["version"]] = Version(
                item["version"], data if data is not None else {}, item["content_hash"]
            )
        if current_data is None:
            raise ImportFormatError(
                f"current version data missing for {raw_entity['id']}"
            )
        stored = Entity(
            key=key,
            current_version=current_version,
            versions=versions,
        )
        if stored.current.content_hash != _content_hash(current_data):
            raise ImportFormatError(
                f"current version content hash mismatch for {raw_entity['id']}"
            )
        store.entities[key] = stored

    for raw_ref in bundle.get("pending_relationships", []):
        store.pending.add(
            PendingReference(
                source_entity=raw_ref["source_entity"],
                field_name=raw_ref["field"],
                target_type=raw_ref["target_type"],
                target=parse_canonical_id(raw_ref["target_entity"]),
            )
        )

    decisions: list[IdentityDecision] = []
    for raw_decision in bundle.get("identity_decisions", []):
        decisions.append(
            IdentityDecision(
                decision_id=raw_decision["decision_id"],
                action=raw_decision["action"],
                group_id=raw_decision["group_id"],
                members=tuple(raw_decision["members"]),
                reason=raw_decision["reason"],
                operator=raw_decision["operator"],
                decided_at=raw_decision["decided_at"],
                supersedes=raw_decision.get("supersedes"),
                evidence=tuple(raw_decision.get("evidence", [])),
            )
        )
    store.load_decisions(decisions)
    return store


def _content_hash(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

