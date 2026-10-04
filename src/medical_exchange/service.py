from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import EntityKey, ExportRecord

if TYPE_CHECKING:
    from .store import RecordStore

UNIFIED_FORMAT = "unified.records.v1"


def export_bundle(store: "RecordStore", label: str = "") -> dict[str, Any]:
    """Export current data, retained versions, and explicit relationship state."""
    from .store import _operation_id, _utc_now

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
                "retracted": store.is_current_retracted(key),
                "data": entity.current.data,
                "versions": [
                    {
                        "version": item.version,
                        "content_hash": item.content_hash,
                        "recorded_at": item.recorded_at,
                        "retracted": store.is_version_retracted(key, item.version),
                        "data": item.data,
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
            status = "resolved" if target_key in store.entities else "pending"
            if target_key in store.entities and store.is_current_retracted(target_key):
                status = "retracted"
            effective_target = target_key
            if target_type == "patient" and target_key in store.patient_groups:
                effective_target = store.canonical_patient(target_key)
            relationships.append(
                {
                    "source_entity": key.canonical_id(),
                    "field": field_name,
                    "target_type": target_type,
                    "target_entity": target_key.canonical_id(),
                    "original_target_entity": target_key.canonical_id(),
                    "canonical_target_entity": effective_target.canonical_id(),
                    "status": status,
                }
            )

    relationships.sort(
        key=lambda item: (item["source_entity"], item["field"], item["target_entity"])
    )
    export_time = _utc_now()
    export_id = _operation_id("export", export_time, len(store.export_records))
    store.export_records.append(
        ExportRecord(
            id=export_id,
            timestamp=export_time,
            label=label,
            versions={
                key.canonical_id(): entity.current_version
                for key, entity in store.entities.items()
            },
        )
    )
    return {
        "format": UNIFIED_FORMAT,
        "export_id": export_id,
        "exported_at": export_time,
        "label": label,
        "sources": [
            {"id": source.id, "name": source.name}
            for source in sorted(store.sources.values(), key=lambda item: item.id)
        ],
        "entities": entities,
        "relationships": relationships,
        "patient_identity_groups": _identity_groups(store),
        "identity_operations": [_identity_operation(item) for item in store.identity_operations],
        "retractions": [
            {
                "id": notice.id,
                "source_id": notice.source_id,
                "type": notice.entity_type,
                "record_id": notice.record_id,
                "version": notice.version,
                "timestamp": notice.timestamp,
                "reason": notice.reason,
                "operator": notice.operator,
                "status": store.notice_status(notice),
            }
            for notice in store.retractions
        ],
        "export_records": [
            {
                "id": record.id,
                "timestamp": record.timestamp,
                "label": record.label,
                "versions": dict(record.versions),
            }
            for record in store.export_records
        ],
        "dispositions": [
            {
                "id": disposition.id,
                "target_kind": disposition.target_kind,
                "target_id": disposition.target_id,
                "notice_id": disposition.notice_id,
                "timestamp": disposition.timestamp,
                "operator": disposition.operator,
                "reason": disposition.reason,
                "source_versions": dict(disposition.source_versions),
                "replacements": [
                    {
                        "entity": item.entity,
                        "from_version": item.from_version,
                        "to_version": item.to_version,
                        "fields": list(item.fields),
                    }
                    for item in disposition.replacements
                ],
                "retained": [
                    {
                        "entity": item.entity,
                        "version": item.version,
                        "restriction": item.restriction,
                    }
                    for item in disposition.retained
                ],
            }
            for disposition in store.dispositions
        ],
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
        "field_decisions": [
            {
                "id": decision.id,
                "export_id": decision.export_id,
                "timestamp": decision.timestamp,
                "operator": decision.operator,
                "reason": decision.reason,
                "choices": {
                    entity_id: dict(fields)
                    for entity_id, fields in decision.choices.items()
                },
            }
            for decision in store.field_decisions
        ],
    }


def _identity_groups(store: "RecordStore") -> list[dict[str, Any]]:
    grouped: dict[str, list[EntityKey]] = {}
    for key, group_id in store.patient_groups.items():
        grouped.setdefault(group_id, []).append(key)
    result = []
    for group_id, members in sorted(grouped.items()):
        ordered = sorted(members, key=lambda item: item.canonical_id())
        canonical = next(
            (
                key
                for key in ordered
                if store.canonical_patient(key).canonical_id() == key.canonical_id()
            ),
            ordered[0],
        )
        result.append(
            {
                "id": group_id,
                "canonical_patient": canonical.canonical_id(),
                "patients": [key.canonical_id() for key in ordered],
            }
        )
    return result


def _identity_operation(operation: Any) -> dict[str, Any]:
    data = {
        "id": operation.id,
        "operation": operation.operation,
        "timestamp": operation.timestamp,
        "patients": [key.canonical_id() for key in operation.patients],
        "group_id": operation.group_id,
        "reason": operation.reason,
        "operator": operation.operator,
        "previous_groups": dict(operation.previous_groups),
        "patient_versions": dict(operation.patient_versions),
    }
    if operation.canonical_patient is not None:
        data["canonical_patient"] = operation.canonical_patient.canonical_id()
    if operation.reverts:
        data["reverts"] = operation.reverts
    return data
