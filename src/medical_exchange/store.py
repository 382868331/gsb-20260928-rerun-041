from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from .exceptions import ConflictError, IdentityError
from .local_format import (
    MEDLOCAL_FORMAT,
    UNIFIED_FORMAT,
    parse_file,
    source_from_bundle,
    validate_envelope,
    validate_record,
)
from .models import Entity, EntityKey, IdentityOperation, PendingReference, Source, Version

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

    def patient_records(self, patient: str | EntityKey) -> dict[str, list[Entity]]:
        canonical = self.canonical_patient(patient)
        group = set(self.patient_group(canonical))
        encounters = []
        observations = []
        encounter_patients: dict[EntityKey, EntityKey] = {}

        for key, entity in self.entities.items():
            if key.entity_type != "encounter":
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

    def undo_merge(self, merge_operation_id: str, **kwargs: Any) -> IdentityOperation:
        return self.revert_merge(merge_operation_id, **kwargs)

    def unmerge_patients(self, merge_operation_id: str, **kwargs: Any) -> IdentityOperation:
        return self.revert_merge(merge_operation_id, **kwargs)

    def query_patient_records(self, patient: str | EntityKey) -> dict[str, list[Entity]]:
        return self.patient_records(patient)

    def unified_patient_records(self, patient: str | EntityKey) -> dict[str, list[Entity]]:
        return self.patient_records(patient)

    def _put_version(
        self,
        source_id: str,
        local_id: str,
        entity_type: str,
        version: int,
        data: dict[str, Any],
    ) -> str:
        key = EntityKey(source_id, entity_type, local_id)
        content_hash = _content_hash(data)
        entity = self.entities.get(key)

        if entity is None:
            self.entities[key] = Entity(
                key=key,
                current_version=version,
                versions={version: Version(version, data, content_hash)},
            )
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

        entity.versions[version] = Version(version, data, content_hash)
        entity.current_version = version
        return "accepted"

    def _restore_version(
        self,
        source_id: str,
        local_id: str,
        entity_type: str,
        version: int,
        data: dict[str, Any],
        expected_hash: str | None = None,
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
                versions={version: Version(version, data, content_hash)},
            )
            return "accepted"

        existing = entity.versions.get(version)
        if existing is not None:
            if existing.content_hash == content_hash:
                return "duplicate"
            raise ConflictError(
                f"{entity_type} {local_id!r} version {version} has conflicting content"
            )

        entity.versions[version] = Version(version, data, content_hash)
        entity.current_version = max(entity.current_version, version)
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
                outcomes = [self._restore_unified_entity_version(key, item)]
                current_version = int(item.get("current_version", item.get("version")))
                for historical in item.get("versions", []):
                    if not isinstance(historical, dict):
                        raise ValueError("entity version entries must be objects")
                    if "data" not in historical:
                        continue
                    if int(historical["version"]) == current_version:
                        continue
                    outcomes.append(self._restore_unified_entity_version(key, historical))
            except (KeyError, TypeError, ValueError) as exc:
                result.rejected += 1
                result.failures.append(RecordFailure(entity_id, str(exc)))
                continue
            result.accepted += sum(outcome == "accepted" for outcome in outcomes)
            result.duplicate += sum(outcome == "duplicate" for outcome in outcomes)

        for raw_operation in bundle.get("identity_operations", []):
            self._import_identity_operation(raw_operation)
        self._replay_identity_groups()

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
        )

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
