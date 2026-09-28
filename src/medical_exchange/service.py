from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import EntityKey

if TYPE_CHECKING:
    from .store import RecordStore

UNIFIED_FORMAT = "unified.records.v1"


def export_bundle(store: "RecordStore") -> dict[str, Any]:
    """Export current data, retained versions, and explicit relationship state."""
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
                    {"version": item.version, "content_hash": item.content_hash}
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
    }
