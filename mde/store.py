from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .errors import ConflictError, MDEError, ValidationError
from .format import Envelope, iter_refs, parse_envelope, parse_file
from .model import Encounter, Patient, Record, Ref, Study


@dataclass
class Failure:
    uid: str | None
    reason: str


@dataclass
class BatchResult:
    source_id: str
    batch_id: str
    imported: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    duplicate_batch: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def failure(self) -> Failure | None:
        return self.failures[0] if self.failures else None


def _fingerprint(record: Record) -> str:
    body = json.dumps(record.payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"{record.type}|{body}"


class RecordStore:
    """Unified record store keyed by (source, type, local id).

    Updates never create a second entity: identity comes from the source
    namespace id, never from names. Missing references stay pending until the
    target arrives, even for out-of-order delivery.
    """

    def __init__(self) -> None:
        self._records: dict[str, Record] = {}
        self._versions: dict[str, int] = {}
        self._fingerprints: dict[str, str] = {}
        # target key (source, ref_type, local_id) -> {(owner_uid, field)}
        self._pending: dict[tuple[str, str, str], set[tuple[str, str]]] = {}
        self._batches: set[tuple[str, str]] = set()

    def import_envelope(self, raw: Any) -> BatchResult:
        try:
            envelope = parse_envelope(raw)
        except ValidationError as exc:
            result = BatchResult(source_id="?", batch_id="?")
            result.failures.append(Failure(None, str(exc)))
            return result
        return self.apply_envelope(envelope)

    def import_file(self, path: str) -> BatchResult:
        envelope = parse_file(path)
        return self.apply_envelope(envelope)

    def apply_envelope(self, envelope: Envelope) -> BatchResult:
        result = BatchResult(envelope.source_id, envelope.batch_id)
        key = envelope.batch_key
        if key in self._batches:
            # Re-delivering the same batch must not double-count anything.
            result.duplicate_batch = True
            result.duplicates.extend(r.uid for r in envelope.records)
            return result

        # parse_envelope already enforced the format contract before we get
        # here; missing reference targets are allowed and become pending.
        self._batches.add(key)
        for record in envelope.records:
            try:
                outcome = self._apply(record)
            except MDEError as exc:
                result.failures.append(Failure(record.uid, str(exc)))
                continue
            bucket = {
                "imported": result.imported,
                "updated": result.updated,
                "duplicate": result.duplicates,
                "stale": result.stale,
            }[outcome]
            bucket.append(record.uid)
            self._park_or_resolve(record)
        return result

    def has(self, source_id: str, ref_type: str, local_id: str) -> bool:
        return f"{source_id}:{ref_type}:{local_id}" in self._records

    def get(self, uid: str) -> Record | None:
        return self._records.get(uid)

    def _apply(self, record: Record) -> str:
        uid = record.uid
        current_version = self._versions.get(uid)
        if current_version is None:
            self._put(record)
            return "imported"
        if record.version < current_version:
            return "stale"
        if record.version == current_version:
            if _fingerprint(record) == self._fingerprints[uid]:
                return "duplicate"
            raise ConflictError(uid, record.version)
        self._put(record)
        return "updated"

    def _put(self, record: Record) -> None:
        self._records[record.uid] = record
        self._versions[record.uid] = record.version
        self._fingerprints[record.uid] = _fingerprint(record)

    def _park_or_resolve(self, record: Record) -> None:
        # This record may satisfy relations parked earlier.
        target_key = (record.source_id, record.type, record.local_id)
        self._pending.pop(target_key, None)
        for field_name, ref in iter_refs(record):
            if self.has(record.source_id, ref.ref_type, ref.local_id):
                continue
            key = (record.source_id, ref.ref_type, ref.local_id)
            self._pending.setdefault(key, set()).add((record.uid, field_name))

    def is_resolved(self, record: Record) -> bool:
        for _field, ref in iter_refs(record):
            if not self.has(record.source_id, ref.ref_type, ref.local_id):
                return False
        return True

    def pending_relations(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for (source_id, ref_type, local_id), owners in sorted(self._pending.items()):
            for owner_uid, field_name in sorted(owners):
                rows.append(
                    {
                        "owner": owner_uid,
                        "field": field_name,
                        "missing": f"{source_id}:{ref_type}:{local_id}",
                    }
                )
        return rows

    def export_unified(self) -> dict[str, Any]:
        """Export the unified graph with stable uids and resolved links."""
        patients: list[dict[str, Any]] = []
        encounters: list[dict[str, Any]] = []
        studies: list[dict[str, Any]] = []
        for uid in sorted(self._records):
            record = self._records[uid]
            node = {
                "uid": uid,
                "version": record.version,
                "data": record.payload,
            }
            links = {}
            for field_name, ref in iter_refs(record):
                target_uid = (
                    f"{record.source_id}:{ref.ref_type}:{ref.local_id}"
                    if self.has(record.source_id, ref.ref_type, ref.local_id)
                    else None
                )
                links[field_name] = target_uid
            node["links"] = links
            if isinstance(record, Patient):
                patients.append(node)
            elif isinstance(record, Encounter):
                encounters.append(node)
            elif isinstance(record, Study):
                studies.append(node)
        return {
            "format": "mde-unified",
            "format_version": 1,
            "patients": patients,
            "encounters": encounters,
            "studies": studies,
            "pending_relations": self.pending_relations(),
        }

    def export_source_envelope(self, source_id: str, batch_id: str = "export") -> dict[str, Any]:
        """Round-trip export back into mde-json, using local ids and refs."""
        groups: dict[str, list[dict[str, Any]]] = {
            "patient": [],
            "encounter": [],
            "study": [],
        }
        for uid in sorted(self._records):
            record = self._records[uid]
            if record.source_id != source_id:
                continue
            if not self.is_resolved(record):
                continue
            item: dict[str, Any] = {
                "id": record.local_id,
                "version": record.version,
                **record.payload,
            }
            groups[record.type].append(item)
        for items in groups.values():
            items.sort(key=lambda row: row["id"])
        return {
            "format": "mde-json",
            "format_version": 1,
            "source_id": source_id,
            "batch_id": batch_id,
            "records": groups,
        }
