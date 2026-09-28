from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .exceptions import ConflictError
from .local_format import parse_file, source_from_bundle, validate_envelope, validate_record
from .models import Entity, EntityKey, PendingReference, Source, Version

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
    failures: list[RecordFailure] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "duplicate": self.duplicate,
            "resolved_references": self.resolved_references,
            "pending_references": self.pending_references,
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

    def import_bundle(self, bundle: dict[str, Any]) -> ImportResult:
        validate_envelope(bundle)
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
        result.resolved_references = resolved
        result.pending_references = pending
        return result

    def import_file(self, path: str) -> ImportResult:
        return self.import_bundle(parse_file(path))

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

        existing_still_pending = sum(
            1 for ref in self.pending if ref.target.source_id == source_id
        )
        return resolved, existing_still_pending


def _content_hash(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def import_bundle(bundle: dict[str, Any], store: RecordStore | None = None) -> tuple[RecordStore, ImportResult]:
    record_store = store or RecordStore()
    return record_store, record_store.import_bundle(bundle)


def import_file(path: str, store: RecordStore | None = None) -> tuple[RecordStore, ImportResult]:
    record_store = store or RecordStore()
    return record_store, record_store.import_file(path)
