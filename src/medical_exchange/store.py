from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Iterable

from .exceptions import ConflictError, DispositionError, IdentityError
from .local_format import (
    MEDLOCAL_FORMAT,
    UNIFIED_FORMAT,
    parse_file,
    source_from_bundle,
    validate_envelope,
    validate_record,
)
from .models import (
    Disposition,
    DispositionReplacement,
    DispositionRestriction,
    Entity,
    EntityKey,
    ExportRecord,
    IdentityOperation,
    PendingReference,
    RetractionNotice,
    Source,
    Version,
)

REFERENCE_FIELDS = {
    "encounter": (("patient_ref", "patient"),),
    "observation": (("patient_ref", "patient"), ("encounter_ref", "encounter")),
}


@dataclass
class RecordFailure:
    record_id: str | None
    reason: str


@dataclass
class ImportResult:
    source: str
    accepted: int = 0
    rejected: int = 0
    duplicate: int = 0
    resolved_references: int = 0
    pending_references: int = 0
    manual_decisions_preserved: int = 0
    retractions_applied: int = 0
    retractions_pending: int = 0
    retractions_duplicate: int = 0
    dispositions_imported: int = 0
    dispositions_duplicate: int = 0
    failures: list[RecordFailure] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "duplicate": self.duplicate,
            "resolved_references": self.resolved_references,
            "pending_references": self.pending_references,
            "manual_decisions_preserved": self.manual_decisions_preserved,
            "retractions_applied": self.retractions_applied,
            "retractions_pending": self.retractions_pending,
            "retractions_duplicate": self.retractions_duplicate,
            "dispositions_imported": self.dispositions_imported,
            "dispositions_duplicate": self.dispositions_duplicate,
            "failures": [
                {"record_id": item.record_id, "reason": item.reason}
                for item in self.failures
            ],
        }


class RecordStore:
    def __init__(self) -> None:
        self.sources: dict[str, Source] = {}
        self.entities: dict[EntityKey, Entity] = {}
        self.pending: set[PendingReference] = set()
        self.identity_operations: list[IdentityOperation] = []
        self.patient_groups: dict[EntityKey, str] = {}
        self.retractions: list[RetractionNotice] = []
        self.export_records: list[ExportRecord] = []
        self.dispositions: list[Disposition] = []
        self._retracted_versions: set[tuple[EntityKey, int]] = set()

    def import_bundle(self, bundle: dict[str, Any]) -> ImportResult:
        validate_envelope(bundle)
        if bundle.get("format") == UNIFIED_FORMAT:
            return self._import_unified(bundle)
        source = source_from_bundle(bundle)
        self.sources.setdefault(source.id, source)
        result = ImportResult(source=source.id)

        for raw_record in bundle["records"]:
            record_id = raw_record.get("id") if isinstance(raw_record, dict) else None
            try:
                local_id, entity_type, version, data = validate_record(raw_record)
                outcome = self._put_version(source.id, local_id, entity_type, version, data)
            except (ValueError, TypeError) as exc:
                result.rejected += 1
                result.failures.append(RecordFailure(record_id, str(exc)))
                continue

            if outcome == "duplicate":
                result.duplicate += 1
            else:
                result.accepted += 1

        for raw_notice in bundle.get("retractions", []):
            try:
                _, outcome = self._apply_retraction(raw_notice, source.id)
            except (ValueError, TypeError) as exc:
                result.rejected += 1
                notice_id = raw_notice.get("record_id") if isinstance(raw_notice, dict) else None
                result.failures.append(RecordFailure(notice_id, str(exc)))
                continue
            if outcome == "duplicate":
                result.retractions_duplicate += 1
            elif outcome == "applied":
                result.retractions_applied += 1
            else:
                result.retractions_pending += 1

        resolved, pending = self._rebuild_references(source.id)
        self._replay_identity_groups()
        result.resolved_references = resolved
        result.pending_references = pending
        result.manual_decisions_preserved = self._active_merge_count()
        return result

    def import_file(self, path: str) -> ImportResult:
        return self.import_bundle(parse_file(path))

    def merge_patients(
        self,
        first_patient: str | EntityKey,
        second_patient: str | EntityKey,
        *,
        reason: str = "",
        operator: str = "",
        timestamp: str | None = None,
        operation_id: str | None = None,
    ) -> IdentityOperation:
        first = self._patient_key(first_patient)
        second = self._patient_key(second_patient)
        if first == second:
            raise IdentityError("cannot merge a patient with itself")
        self._require_existing_patient(first)
        self._require_existing_patient(second)
        if self.patient_groups.get(first) == self.patient_groups.get(second):
            raise IdentityError("patients are already in the same identity group")

        event_time = timestamp or _utc_now()
        operation_id = operation_id or _operation_id("merge", event_time, len(self.identity_operations))
        if any(item.id == operation_id for item in self.identity_operations):
            raise IdentityError(f"identity operation {operation_id!r} already exists")

        canonical = min(first, second, key=lambda item: item.canonical_id())
        group_id = f"identity-group/{operation_id}"
        previous_groups = {
            key.canonical_id(): self.patient_groups[key]
            for key in (first, second)
        }
        patient_versions = {
            key.canonical_id(): self.entities[key].current_version
            for key in (first, second)
        }
        operation = IdentityOperation(
            id=operation_id,
            operation="merge",
            timestamp=event_time,
            patients=(first, second),
            group_id=group_id,
            canonical_patient=canonical,
            reason=reason,
            operator=operator,
            previous_groups=previous_groups,
            patient_versions=patient_versions,
        )
        self.identity_operations.append(operation)
        self._replay_identity_groups()
        return operation

    def revert_merge(
        self,
        merge_operation_id: str,
        *,
        reason: str = "",
        operator: str = "",
        timestamp: str | None = None,
        operation_id: str | None = None,
    ) -> IdentityOperation:
        merge = next(
            (
                item
                for item in self.identity_operations
                if item.operation == "merge" and item.id == merge_operation_id
            ),
            None,
        )
        if merge is None:
            raise IdentityError(f"merge operation {merge_operation_id!r} does not exist")
        if any(item.reverts == merge_operation_id for item in self.identity_operations):
            raise IdentityError(f"merge operation {merge_operation_id!r} was already reverted")

        event_time = timestamp or _utc_now()
        operation_id = operation_id or _operation_id("revert", event_time, len(self.identity_operations))
        operation = IdentityOperation(
            id=operation_id,
            operation="revert_merge",
            timestamp=event_time,
            patients=merge.patients,
            group_id=merge.group_id,
            reason=reason,
            operator=operator,
            previous_groups=dict(merge.previous_groups),
            reverts=merge_operation_id,
            patient_versions=dict(merge.patient_versions),
        )
        self.identity_operations.append(operation)
        self._replay_identity_groups()
        return operation

    def canonical_patient(self, patient: str | EntityKey) -> EntityKey:
        key = self._patient_key(patient)
        self._require_existing_patient(key)
        group_id = self.patient_groups[key]
        return min(
            (item for item, group in self.patient_groups.items() if group == group_id),
            key=lambda item: item.canonical_id(),
        )

    def patient_group(self, patient: str | EntityKey) -> list[EntityKey]:
        canonical = self.canonical_patient(patient)
        group_id = self.patient_groups[canonical]
        return sorted(
            (key for key, group in self.patient_groups.items() if group == group_id),
            key=lambda item: item.canonical_id(),
        )

    def patient_records(
        self, patient: str | EntityKey, *, include_retracted: bool = False
    ) -> dict[str, list[Entity]]:
        canonical = self.canonical_patient(patient)
        group = set(self.patient_group(canonical))
        encounters = []
        observations = []
        encounter_patients: dict[EntityKey, EntityKey] = {}

        for key, entity in self.entities.items():
            if key.entity_type != "encounter":
                continue
            if not include_retracted and self.is_current_retracted(key):
                continue
            patient_key = EntityKey(
                key.source_id, "patient", entity.current.data["patient_ref"]
            )
            resolved = self.canonical_patient(patient_key) if patient_key in self.patient_groups else patient_key
            encounter_patients[key] = resolved
            if resolved == canonical or patient_key in group:
                encounters.append(entity)

        for key, entity in self.entities.items():
            if key.entity_type != "observation":
                continue
            if not include_retracted and self.is_current_retracted(key):
                continue
            patient_key = EntityKey(
                key.source_id, "patient", entity.current.data["patient_ref"]
            )
            encounter_key = EntityKey(
                key.source_id, "encounter", entity.current.data["encounter_ref"]
            )
            patient_matches = patient_key in group or (
                patient_key in self.patient_groups and self.canonical_patient(patient_key) == canonical
            )
            encounter_matches = encounter_patients.get(encounter_key) == canonical
            if patient_matches or encounter_matches:
                observations.append(entity)

        return {
            "patient": [self.entities[canonical]],
            "encounters": sorted(encounters, key=lambda item: item.key.canonical_id()),
            "observations": sorted(observations, key=lambda item: item.key.canonical_id()),
        }

    def merge_patient_identities(self, first_patient: str | EntityKey, second_patient: str | EntityKey, **kwargs: Any) -> IdentityOperation:
        return self.merge_patients(first_patient, second_patient, **kwargs)

    def retract_version(
        self,
        source_id: str,
        entity_type: str,
        record_id: str,
        version: int,
        *,
        reason: str = "",
        operator: str = "",
        timestamp: str | None = None,
        notice_id: str | None = None,
    ) -> tuple[RetractionNotice, bool]:
        notice, outcome = self._apply_retraction(
            {
                "id": notice_id,
                "record_id": record_id,
                "type": entity_type,
                "version": version,
                "reason": reason,
                "operator": operator,
                "timestamp": timestamp,
            },
            source_id,
        )
        return notice, outcome != "duplicate"

    def _apply_retraction(
        self, raw: Any, source_id: str
    ) -> tuple[RetractionNotice, str]:
        if not isinstance(raw, dict):
            raise ValueError("retraction notice must be an object")
        record_id = raw.get("record_id")
        entity_type = raw.get("type")
        version = raw.get("version")
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("retraction record_id is required")
        if entity_type not in ("patient", "encounter", "observation"):
            raise ValueError("retraction type must be patient, encounter, or observation")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("retraction version must be a positive integer")
        notice_id = raw.get("id")
        if notice_id is not None and not isinstance(notice_id, str):
            raise ValueError("retraction id must be a string")
        timestamp = raw.get("timestamp") or _utc_now()
        reason = raw.get("reason") or ""
        operator = raw.get("operator") or ""

        for existing in self.retractions:
            if notice_id and existing.id == notice_id:
                return existing, "duplicate"
            if (
                existing.source_id == source_id
                and existing.entity_type == entity_type
                and existing.record_id == record_id
                and existing.version == version
            ):
                return existing, "duplicate"

        notice = RetractionNotice(
            id=notice_id or _operation_id("retract", timestamp, len(self.retractions)),
            source_id=source_id,
            entity_type=entity_type,
            record_id=record_id,
            version=version,
            timestamp=timestamp,
            reason=reason,
            operator=operator,
        )
        self.retractions.append(notice)
        self._replay_retractions()
        return notice, self.notice_status(notice)

    def _replay_retractions(self) -> None:
        retracted: set[tuple[EntityKey, int]] = set()
        for notice in self.retractions:
            key = notice.entity_key()
            entity = self.entities.get(key)
            if entity is not None and notice.version in entity.versions:
                retracted.add((key, notice.version))
        self._retracted_versions = retracted

    def notice_status(self, notice: RetractionNotice) -> str:
        if (notice.entity_key(), notice.version) in self._retracted_versions:
            return "applied"
        return "pending"

    def pending_retractions(self) -> list[RetractionNotice]:
        return [
            notice for notice in self.retractions if self.notice_status(notice) == "pending"
        ]

    def is_version_retracted(self, key: EntityKey, version: int) -> bool:
        return (key, version) in self._retracted_versions

    def is_current_retracted(self, key: EntityKey) -> bool:
        entity = self.entities.get(key)
        if entity is None:
            return False
        return (key, entity.current_version) in self._retracted_versions

    def retracted_entities(self) -> list[Entity]:
        return [
            entity
            for key, entity in sorted(
                self.entities.items(), key=lambda item: item[0].canonical_id()
            )
            if self.is_current_retracted(key)
        ]

    def entity_timeline(self, entity_id: str | EntityKey) -> list[dict[str, Any]]:
        key = (
            entity_id
            if isinstance(entity_id, EntityKey)
            else self._entity_key_from_id(entity_id)
        )
        events: list[dict[str, Any]] = []
        entity = self.entities.get(key)
        if entity is not None:
            for version in sorted(entity.versions.values(), key=lambda item: item.version):
                events.append(
                    {
                        "event": "version_recorded",
                        "version": version.version,
                        "timestamp": version.recorded_at,
                        "content_hash": version.content_hash,
                        "retracted": self.is_version_retracted(key, version.version),
                    }
                )
        for notice in self.retractions:
            if notice.entity_key() == key:
                events.append(
                    {
                        "event": "retraction_notice",
                        "version": notice.version,
                        "timestamp": notice.timestamp,
                        "notice_id": notice.id,
                        "status": self.notice_status(notice),
                        "reason": notice.reason,
                        "operator": notice.operator,
                    }
                )
        for disposition in self.dispositions:
            notice = next(
                (
                    item
                    for item in self.retractions
                    if item.id == disposition.retraction_id
                ),
                None,
            )
            if notice is not None and notice.entity_key() == key:
                events.append(
                    {
                        "event": "disposition",
                        "version": notice.version,
                        "timestamp": disposition.timestamp,
                        "disposition_id": disposition.id,
                        "target_kind": disposition.target_kind,
                        "target_id": disposition.target_id,
                        "operator": disposition.operator,
                        "reason": disposition.reason,
                    }
                )
        events.sort(key=lambda item: (item["timestamp"] or "", item["event"]))
        return events

    def visible_entities(self, at: str | None = None) -> dict[EntityKey, Version]:
        visible: dict[EntityKey, Version] = {}
        for key, entity in self.entities.items():
            if at is None:
                current = entity.current
                if not self.is_version_retracted(key, current.version):
                    visible[key] = current
                continue
            candidates = [
                version
                for version in entity.versions.values()
                if not version.recorded_at or version.recorded_at <= at
            ]
            retracted_at = {
                notice.version
                for notice in self.retractions
                if notice.entity_key() == key and notice.timestamp <= at
            }
            valid = [
                version for version in candidates if version.version not in retracted_at
            ]
            if valid:
                newest = max(valid, key=lambda item: item.version)
                visible[key] = newest
        return visible

    def retraction_impact(
        self, source_id: str, entity_type: str, record_id: str, version: int
    ) -> dict[str, Any]:
        key = EntityKey(source_id, entity_type, record_id)
        canonical_id = key.canonical_id()
        notices = [
            notice
            for notice in self.retractions
            if notice.entity_key() == key and notice.version == version
        ]
        if not notices:
            raise ValueError(f"no retraction notice for {canonical_id!r} version {version}")

        merges = []
        if entity_type == "patient":
            for operation in self.identity_operations:
                if operation.patient_versions.get(canonical_id) == version:
                    merges.append(operation)

        exports = [
            record
            for record in self.export_records
            if record.versions.get(canonical_id) == version
        ]

        dependents = []
        for entity_key, entity in self.entities.items():
            for field_name, target_type in REFERENCE_FIELDS.get(
                entity_key.entity_type, ()
            ):
                if entity_key.source_id != source_id or target_type != entity_type:
                    continue
                if entity.current.data.get(field_name) == record_id:
                    dependents.append(entity_key.canonical_id())

        return {
            "entity": canonical_id,
            "version": version,
            "status": self.notice_status(notices[0]),
            "notices": [notice.id for notice in notices],
            "identity_operations": [operation.id for operation in merges],
            "exports": [record.id for record in exports],
            "dependent_entities": sorted(dependents),
        }

    def undo_merge(self, merge_operation_id: str, **kwargs: Any) -> IdentityOperation:
        return self.revert_merge(merge_operation_id, **kwargs)

    def propose_disposition(
        self,
        target_kind: str,
        target_id: str,
        *,
        retraction_id: str,
        replacements: Iterable[dict[str, Any]] = (),
        restrictions: Iterable[dict[str, Any]] = (),
        reason: str = "",
        operator: str = "",
        timestamp: str | None = None,
        disposition_id: str | None = None,
    ) -> Disposition:
        notice = next(
            (item for item in self.retractions if item.id == retraction_id), None
        )
        if notice is None:
            raise DispositionError(f"retraction notice {retraction_id!r} does not exist")
        source_versions, original_choices = self._target_evidence(target_kind, target_id)

        parsed_replacements = tuple(
            self._validate_replacement(item) for item in replacements
        )
        parsed_restrictions = tuple(
            self._validate_restriction(item) for item in restrictions
        )
        if not parsed_replacements and not parsed_restrictions:
            raise DispositionError(
                "disposition must replace or restrict at least one field"
            )

        event_time = timestamp or _utc_now()
        disposition_id = disposition_id or _operation_id(
            "disposition", event_time, len(self.dispositions)
        )
        existing = next(
            (item for item in self.dispositions if item.id == disposition_id), None
        )
        if existing is not None:
            return existing

        disposition = Disposition(
            id=disposition_id,
            target_kind=target_kind,
            target_id=target_id,
            retraction_id=retraction_id,
            operator=operator,
            timestamp=event_time,
            reason=reason,
            source_versions=source_versions,
            original_choices=original_choices,
            replacements=parsed_replacements,
            restrictions=parsed_restrictions,
        )
        self.dispositions.append(disposition)
        return disposition

    def _target_evidence(
        self, target_kind: str, target_id: str
    ) -> tuple[dict[str, int], dict[str, Any]]:
        if target_kind == "identity_operation":
            operation = next(
                (item for item in self.identity_operations if item.id == target_id),
                None,
            )
            if operation is None:
                raise DispositionError(
                    f"identity operation {target_id!r} does not exist"
                )
            choices: dict[str, Any] = {
                "operation": operation.operation,
                "group_id": operation.group_id,
                "patients": [key.canonical_id() for key in operation.patients],
            }
            if operation.canonical_patient is not None:
                choices["canonical_patient"] = operation.canonical_patient.canonical_id()
            if operation.reverts:
                choices["reverts"] = operation.reverts
            return dict(operation.patient_versions), choices
        if target_kind == "export":
            record = next(
                (item for item in self.export_records if item.id == target_id), None
            )
            if record is None:
                raise DispositionError(f"export record {target_id!r} does not exist")
            return dict(record.versions), {
                "label": record.label,
                "versions": dict(record.versions),
            }
        raise DispositionError(
            "disposition target_kind must be 'identity_operation' or 'export'"
        )

    def _validate_replacement(self, raw: dict[str, Any]) -> DispositionReplacement:
        if not isinstance(raw, dict):
            raise DispositionError("replacement must be an object")
        field_name = raw.get("field")
        if not isinstance(field_name, str) or not field_name.strip():
            raise DispositionError("replacement field is required")
        entity_id = raw.get("replacement_entity")
        version = raw.get("replacement_version")
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise DispositionError("replacement_entity is required")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise DispositionError("replacement_version must be a positive integer")
        key = self._entity_key_from_id(entity_id)
        entity = self.entities.get(key)
        if entity is None or version not in entity.versions:
            raise DispositionError(
                f"replacement version {entity_id!r} v{version} does not exist"
            )
        if self.is_version_retracted(key, version):
            raise DispositionError(
                f"replacement version {entity_id!r} v{version} is retracted"
            )
        scope = raw.get("scope") or ""
        if not isinstance(scope, str):
            raise DispositionError("replacement scope must be a string")
        return DispositionReplacement(
            field=field_name,
            replacement_entity=key.canonical_id(),
            replacement_version=version,
            scope=scope,
        )

    def _validate_restriction(self, raw: dict[str, Any]) -> DispositionRestriction:
        if not isinstance(raw, dict):
            raise DispositionError("restriction must be an object")
        field_name = raw.get("field")
        restriction = raw.get("restriction")
        if not isinstance(field_name, str) or not field_name.strip():
            raise DispositionError("restriction field is required")
        if not isinstance(restriction, str) or not restriction.strip():
            raise DispositionError("restriction must name the usage limit")
        scope = raw.get("scope") or ""
        if not isinstance(scope, str):
            raise DispositionError("restriction scope must be a string")
        return DispositionRestriction(
            field=field_name, restriction=restriction, scope=scope
        )

    def dispositions_for(
        self, target_kind: str, target_id: str
    ) -> list[Disposition]:
        return [
            item
            for item in self.dispositions
            if item.target_kind == target_kind and item.target_id == target_id
        ]

    def disposition_conflicts(
        self, target_kind: str, target_id: str
    ) -> list[dict[str, Any]]:
        actions: dict[tuple[str, str], list[tuple[Disposition, tuple[Any, ...]]]] = {}
        for disposition in self.dispositions_for(target_kind, target_id):
            for replacement in disposition.replacements:
                key = (replacement.field, replacement.scope)
                action = (
                    "replace",
                    replacement.replacement_entity,
                    replacement.replacement_version,
                )
                actions.setdefault(key, []).append((disposition, action))
            for restriction in disposition.restrictions:
                key = (restriction.field, restriction.scope)
                action = ("restrict", restriction.restriction)
                actions.setdefault(key, []).append((disposition, action))

        conflicts = []
        for (field_name, scope), entries in sorted(actions.items()):
            distinct = {action for _, action in entries}
            if len(distinct) < 2:
                continue
            conflicts.append(
                {
                    "field": field_name,
                    "scope": scope,
                    "dispositions": [item.id for item, _ in entries],
                    "operators": [item.operator for item, _ in entries],
                    "reasons": [item.reason for item, _ in entries],
                    "actions": [list(action) for _, action in entries],
                }
            )
        return conflicts

    def compare_disposition(self, disposition_id: str) -> dict[str, Any]:
        disposition = self._require_disposition(disposition_id)
        notice = next(
            item for item in self.retractions if item.id == disposition.retraction_id
        )
        key = notice.entity_key()
        entity = self.entities.get(key)
        old_version = (
            entity.versions.get(notice.version) if entity is not None else None
        )
        items = []
        for replacement in disposition.replacements:
            replacement_key = self._entity_key_from_id(replacement.replacement_entity)
            new_version = self.entities[replacement_key].versions[
                replacement.replacement_version
            ]
            items.append(
                {
                    "field": replacement.field,
                    "scope": replacement.scope,
                    "before": (
                        old_version.data.get(replacement.field)
                        if old_version is not None
                        else None
                    ),
                    "after": new_version.data.get(replacement.field),
                    "replacement_entity": replacement.replacement_entity,
                    "replacement_version": replacement.replacement_version,
                }
            )
        return {
            "disposition": disposition.id,
            "retraction": notice.id,
            "entity": key.canonical_id(),
            "version": notice.version,
            "replacements": items,
        }

    def disposition_view(self, target_kind: str, target_id: str) -> dict[str, Any]:
        dispositions = self.dispositions_for(target_kind, target_id)
        conflicts = {
            (item["field"], item["scope"])
            for item in self.disposition_conflicts(target_kind, target_id)
        }
        affected: dict[tuple[str, str], dict[str, Any]] = {}
        for disposition in dispositions:
            notice = next(
                item
                for item in self.retractions
                if item.id == disposition.retraction_id
            )
            key = notice.entity_key()
            entity = self.entities.get(key)
            old_version = (
                entity.versions.get(notice.version) if entity is not None else None
            )
            old_data = old_version.data if old_version is not None else {}
            for field_name in old_data:
                entry = affected.setdefault(
                    (field_name, ""),
                    {"field": field_name, "scope": "", "status": "pending"},
                )
                entry.setdefault("retracted_value", old_data.get(field_name))
            for replacement in disposition.replacements:
                replacement_key = self._entity_key_from_id(
                    replacement.replacement_entity
                )
                new_version = self.entities[replacement_key].versions[
                    replacement.replacement_version
                ]
                affected[(replacement.field, replacement.scope)] = {
                    "field": replacement.field,
                    "scope": replacement.scope,
                    "status": "replaced",
                    "retracted_value": old_data.get(replacement.field),
                    "value": new_version.data.get(replacement.field),
                    "replacement_entity": replacement.replacement_entity,
                    "replacement_version": replacement.replacement_version,
                    "decided_by": disposition.id,
                }
            for restriction in disposition.restrictions:
                affected[(restriction.field, restriction.scope)] = {
                    "field": restriction.field,
                    "scope": restriction.scope,
                    "status": "restricted",
                    "retracted_value": old_data.get(restriction.field),
                    "restriction": restriction.restriction,
                    "decided_by": disposition.id,
                }
        for field_name, scope in conflicts:
            if (field_name, scope) in affected:
                affected[(field_name, scope)]["status"] = "conflict"
                affected[(field_name, scope)]["decided_by"] = [
                    item.id
                    for item in dispositions
                    if any(
                        action.field == field_name and action.scope == scope
                        for action in (*item.replacements, *item.restrictions)
                    )
                ]
        return {
            "target_kind": target_kind,
            "target_id": target_id,
            "fields": [
                affected[key] for key in sorted(affected, key=lambda item: (item[0], item[1]))
            ],
            "decisions": [
                {
                    "id": item.id,
                    "operator": item.operator,
                    "timestamp": item.timestamp,
                    "reason": item.reason,
                    "retraction_id": item.retraction_id,
                }
                for item in dispositions
            ],
        }

    def _require_disposition(self, disposition_id: str) -> Disposition:
        disposition = next(
            (item for item in self.dispositions if item.id == disposition_id), None
        )
        if disposition is None:
            raise DispositionError(f"disposition {disposition_id!r} does not exist")
        return disposition

    def unmerge_patients(self, merge_operation_id: str, **kwargs: Any) -> IdentityOperation:
        return self.revert_merge(merge_operation_id, **kwargs)

    def query_patient_records(self, patient: str | EntityKey, **kwargs: Any) -> dict[str, list[Entity]]:
        return self.patient_records(patient, **kwargs)

    def unified_patient_records(self, patient: str | EntityKey, **kwargs: Any) -> dict[str, list[Entity]]:
        return self.patient_records(patient, **kwargs)

    def _put_version(
        self,
        source_id: str,
        local_id: str,
        entity_type: str,
        version: int,
        data: dict[str, Any],
        recorded_at: str = "",
    ) -> str:
        key = EntityKey(source_id, entity_type, local_id)
        content_hash = _content_hash(data)
        recorded_at = recorded_at or _utc_now()
        entity = self.entities.get(key)

        if entity is None:
            self.entities[key] = Entity(
                key=key,
                current_version=version,
                versions={version: Version(version, data, content_hash, recorded_at)},
            )
            self._replay_retractions()
            return "accepted"

        existing = entity.versions.get(version)
        if existing is not None:
            if existing.content_hash == content_hash:
                return "duplicate"
            raise ConflictError(
                f"{entity_type} {local_id!r} version {version} has conflicting content"
            )

        if version <= entity.current_version:
            raise ConflictError(
                f"{entity_type} {local_id!r} version {version} is older than current version "
                f"{entity.current_version}"
            )

        entity.versions[version] = Version(version, data, content_hash, recorded_at)
        entity.current_version = version
        self._replay_retractions()
        return "accepted"

    def _restore_version(
        self,
        source_id: str,
        local_id: str,
        entity_type: str,
        version: int,
        data: dict[str, Any],
        expected_hash: str | None = None,
        recorded_at: str = "",
    ) -> str:
        key = EntityKey(source_id, entity_type, local_id)
        content_hash = _content_hash(data)
        if expected_hash is not None and expected_hash != content_hash:
            raise ConflictError(
                f"{entity_type} {local_id!r} version {version} has an invalid content hash"
            )
        entity = self.entities.get(key)

        if entity is None:
            self.entities[key] = Entity(
                key=key,
                current_version=version,
                versions={version: Version(version, data, content_hash, recorded_at)},
            )
            self._replay_retractions()
            return "accepted"

        existing = entity.versions.get(version)
        if existing is not None:
            if existing.content_hash == content_hash:
                if recorded_at and not existing.recorded_at:
                    entity.versions[version] = Version(
                        version, data, content_hash, recorded_at
                    )
                return "duplicate"
            raise ConflictError(
                f"{entity_type} {local_id!r} version {version} has conflicting content"
            )

        entity.versions[version] = Version(version, data, content_hash, recorded_at)
        entity.current_version = max(entity.current_version, version)
        self._replay_retractions()
        return "accepted"

    def _rebuild_references(self, source_id: str) -> tuple[int, int]:
        affected = {
            ref
            for ref in self.pending
            if ref.target.source_id == source_id or ref.source_entity.startswith(source_id + "/")
        }
        self.pending.difference_update(affected)

        resolved = 0
        pending = 0
        for key, entity in self.entities.items():
            if key.source_id != source_id:
                continue
            for field_name, target_type in REFERENCE_FIELDS.get(key.entity_type, ()):
                target_local_id = entity.current.data[field_name]
                target_key = EntityKey(source_id, target_type, target_local_id)
                reference = PendingReference(
                    source_entity=key.canonical_id(),
                    field_name=field_name,
                    target_type=target_type,
                    target=target_key,
                )
                if target_key in self.entities:
                    resolved += 1
                else:
                    self.pending.add(reference)
                    pending += 1

        return resolved, pending

    def _import_unified(self, bundle: dict[str, Any]) -> ImportResult:
        result = ImportResult(source="unified")
        for raw_source in bundle.get("sources", []):
            if isinstance(raw_source, dict) and isinstance(raw_source.get("id"), str):
                self.sources.setdefault(
                    raw_source["id"],
                    Source(raw_source["id"], raw_source.get("name") or ""),
                )

        for item in bundle.get("entities", []):
            entity_id = item.get("id") if isinstance(item, dict) else None
            try:
                key = self._entity_key_from_id(item["id"], item["type"])
                self.sources.setdefault(
                    key.source_id,
                    Source(
                        key.source_id,
                        item.get("source", {}).get("name", "") or "",
                    ),
                )
                current_version = int(item.get("current_version", item.get("version")))
                current_item = item
                outcomes = []
                for historical in item.get("versions", []):
                    if not isinstance(historical, dict):
                        raise ValueError("entity version entries must be objects")
                    if "data" not in historical:
                        continue
                    if int(historical["version"]) == current_version:
                        if historical.get("recorded_at") and not item.get("recorded_at"):
                            current_item = dict(item)
                            current_item["recorded_at"] = historical["recorded_at"]
                        continue
                    outcomes.append(self._restore_unified_entity_version(key, historical))
                outcomes.append(self._restore_unified_entity_version(key, current_item))
            except (KeyError, TypeError, ValueError) as exc:
                result.rejected += 1
                result.failures.append(RecordFailure(entity_id, str(exc)))
                continue
            result.accepted += sum(outcome == "accepted" for outcome in outcomes)
            result.duplicate += sum(outcome == "duplicate" for outcome in outcomes)

        for raw_operation in bundle.get("identity_operations", []):
            self._import_identity_operation(raw_operation)
        self._replay_identity_groups()

        for raw_notice in bundle.get("retractions", []):
            if not isinstance(raw_notice, dict):
                continue
            try:
                _, outcome = self._apply_retraction(raw_notice, raw_notice.get("source_id", ""))
            except (ValueError, TypeError):
                continue
            if outcome == "applied":
                result.retractions_applied += 1
            elif outcome == "pending":
                result.retractions_pending += 1
            else:
                result.retractions_duplicate += 1

        for raw_export in bundle.get("export_records", []):
            self._import_export_record(raw_export)

        for raw_disposition in bundle.get("dispositions", []):
            outcome = self._import_disposition(raw_disposition)
            if outcome == "imported":
                result.dispositions_imported += 1
            elif outcome == "duplicate":
                result.dispositions_duplicate += 1

        all_resolved = 0
        all_pending = 0
        for source_id in sorted(self.sources):
            resolved, pending = self._rebuild_references(source_id)
            all_resolved += resolved
            all_pending += pending
        result.resolved_references = all_resolved
        result.pending_references = all_pending
        result.manual_decisions_preserved = self._active_merge_count()
        return result

    def _import_identity_operation(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        operation_id = raw.get("id")
        if not isinstance(operation_id, str) or any(item.id == operation_id for item in self.identity_operations):
            return
        patients = tuple(self._entity_key_from_id(item, "patient") for item in raw.get("patients", []))
        operation = IdentityOperation(
            id=operation_id,
            operation=raw.get("operation", ""),
            timestamp=raw.get("timestamp", ""),
            patients=patients,
            group_id=raw.get("group_id", ""),
            canonical_patient=(
                self._entity_key_from_id(raw["canonical_patient"], "patient")
                if raw.get("canonical_patient")
                else None
            ),
            reason=raw.get("reason", ""),
            operator=raw.get("operator", ""),
            previous_groups=dict(raw.get("previous_groups", {})),
            reverts=raw.get("reverts", ""),
            patient_versions={
                str(patient_id): int(version)
                for patient_id, version in raw.get("patient_versions", {}).items()
            },
        )
        self.identity_operations.append(operation)
        self._replay_identity_groups()

    def _restore_unified_entity_version(self, key: EntityKey, item: dict[str, Any]) -> str:
        data = item.get("data")
        if not isinstance(data, dict):
            raise ValueError("entity version data must be an object")
        version = int(item.get("version", item.get("current_version")))
        if version < 1:
            raise ValueError("entity version must be a positive integer")
        expected_hash = item.get("content_hash")
        if expected_hash is not None and not isinstance(expected_hash, str):
            raise ValueError("entity content_hash must be a string")
        return self._restore_version(
            key.source_id,
            key.source_record_id,
            key.entity_type,
            version,
            data,
            expected_hash,
            item.get("recorded_at") or "",
        )

    def _import_export_record(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        record_id = raw.get("id")
        if not isinstance(record_id, str) or not record_id:
            return
        if any(item.id == record_id for item in self.export_records):
            return
        versions = raw.get("versions", {})
        if not isinstance(versions, dict):
            versions = {}
        self.export_records.append(
            ExportRecord(
                id=record_id,
                timestamp=raw.get("timestamp", "") or "",
                label=raw.get("label", "") or "",
                versions={str(key): int(value) for key, value in versions.items()},
            )
        )

    def _import_disposition(self, raw: Any) -> str:
        if not isinstance(raw, dict):
            return "skipped"
        disposition_id = raw.get("id")
        if not isinstance(disposition_id, str) or not disposition_id:
            return "skipped"
        if any(item.id == disposition_id for item in self.dispositions):
            return "duplicate"
        try:
            disposition = Disposition(
                id=disposition_id,
                target_kind=str(raw.get("target_kind", "")),
                target_id=str(raw.get("target_id", "")),
                retraction_id=str(raw.get("retraction_id", "")),
                operator=str(raw.get("operator", "")),
                timestamp=str(raw.get("timestamp", "")),
                reason=str(raw.get("reason", "")),
                source_versions={
                    str(key): int(value)
                    for key, value in (raw.get("source_versions") or {}).items()
                },
                original_choices=dict(raw.get("original_choices") or {}),
                replacements=tuple(
                    DispositionReplacement(
                        field=str(item.get("field", "")),
                        replacement_entity=str(item.get("replacement_entity", "")),
                        replacement_version=int(item.get("replacement_version", 0)),
                        scope=str(item.get("scope", "") or ""),
                    )
                    for item in (raw.get("replacements") or [])
                    if isinstance(item, dict)
                ),
                restrictions=tuple(
                    DispositionRestriction(
                        field=str(item.get("field", "")),
                        restriction=str(item.get("restriction", "")),
                        scope=str(item.get("scope", "") or ""),
                    )
                    for item in (raw.get("restrictions") or [])
                    if isinstance(item, dict)
                ),
            )
        except (TypeError, ValueError):
            return "skipped"
        self.dispositions.append(disposition)
        return "imported"

    def _replay_identity_groups(self) -> None:
        patient_keys = {key for key in self.entities if key.entity_type == "patient"}
        groups = {key: key.canonical_id() for key in patient_keys}
        reverted = set()
        for item in self.identity_operations:
            if item.operation == "revert_merge" and item.reverts:
                reverted.add(item.reverts)
        for operation in self.identity_operations:
            if operation.operation != "merge" or operation.id in reverted:
                continue
            members = [key for key in operation.patients if key in patient_keys]
            if len(members) < 2:
                continue
            old_groups = {groups[key] for key in members}
            new_group = operation.group_id or operation.id
            for key, current_group in list(groups.items()):
                if current_group in old_groups:
                    groups[key] = new_group
        self.patient_groups = groups

    def _active_merge_count(self) -> int:
        reverted = {
            item.reverts
            for item in self.identity_operations
            if item.operation == "revert_merge" and item.reverts
        }
        return sum(
            1
            for item in self.identity_operations
            if item.operation == "merge" and item.id not in reverted
        )

    def _patient_key(self, value: str | EntityKey) -> EntityKey:
        if isinstance(value, EntityKey):
            return EntityKey(value.source_id, "patient", value.source_record_id)
        return self._entity_key_from_id(value, "patient")

    def _require_existing_patient(self, key: EntityKey) -> None:
        if key.entity_type != "patient" or key not in self.entities:
            raise IdentityError(f"patient {key.canonical_id()!r} does not exist")

    @staticmethod
    def _entity_key_from_id(value: str, expected_type: str | None = None) -> EntityKey:
        parts = value.split("/", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"invalid entity id {value!r}")
        source_id, entity_type, record_id = parts
        if expected_type is not None and entity_type != expected_type:
            raise ValueError(f"entity {value!r} must be a {expected_type}")
        return EntityKey(source_id, entity_type, record_id)


def _content_hash(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _operation_id(prefix: str, timestamp: str, index: int) -> str:
    safe_time = "".join(ch if ch.isalnum() or ch in ("-", ".") else "-" for ch in timestamp)
    return f"{prefix}-{safe_time}-{index}"


def import_bundle(bundle: dict[str, Any], store: RecordStore | None = None) -> tuple[RecordStore, ImportResult]:
    record_store = store or RecordStore()
    return record_store, record_store.import_bundle(bundle)


def import_file(path: str, store: RecordStore | None = None) -> tuple[RecordStore, ImportResult]:
    record_store = store or RecordStore()
    return record_store, record_store.import_file(path)
