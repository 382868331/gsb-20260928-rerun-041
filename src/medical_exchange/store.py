from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .exceptions import ConflictError
from .local_format import parse_file, source_from_bundle, validate_envelope, validate_record
from .models import Entity, EntityKey, IdentityDecision, PendingReference, Source, Version

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
    identity_conflicts: list[dict[str, Any]] = field(default_factory=list)

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
            "identity_conflicts": list(self.identity_conflicts),
        }


class RecordStore:
    def __init__(self) -> None:
        self.sources: dict[str, Source] = {}
        self.entities: dict[EntityKey, Entity] = {}
        self.pending: set[PendingReference] = set()
        self.decisions: list[IdentityDecision] = []

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
                if entity_type == "patient":
                    self._record_identity_conflict(source.id, local_id, result)

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

    def _record_identity_conflict(
        self, source_id: str, local_id: str, result: ImportResult
    ) -> None:
        """Flag source updates that arrive while a human merge is authoritative."""
        canonical = EntityKey(source_id, "patient", local_id).canonical_id()
        for group_id, decision in self._active_groups().items():
            if canonical in decision["members"]:
                result.identity_conflicts.append(
                    {
                        "record_id": canonical,
                        "group_id": group_id,
                        "decision_id": decision["merge_decision_id"],
                        "reason": (
                            "newer source version stored as history; manual identity "
                            "merge remains authoritative and was not changed"
                        ),
                    }
                )

    # ------------------------------------------------------------------
    # Manual patient identity management
    # ------------------------------------------------------------------

    def merge_patients(
        self,
        patient_keys: list[EntityKey | str],
        *,
        reason: str,
        operator: str,
        decided_at: str,
    ) -> IdentityDecision:
        members = self._require_patient_keys(patient_keys)
        if len(set(members)) < 2:
            raise ValueError("merge requires at least two distinct patients")
        active = self._active_groups()
        for canonical in members:
            for group_id, group in active.items():
                if canonical in group["members"]:
                    raise ValueError(
                        f"patient {canonical} is already in active merge {group_id}"
                    )

        merge_count = sum(1 for item in self.decisions if item.action == "merge")
        group_id = f"patient-group-{merge_count + 1}"
        decision = IdentityDecision(
            decision_id=len(self.decisions) + 1,
            action="merge",
            group_id=group_id,
            members=tuple(members),
            reason=reason,
            operator=operator,
            decided_at=decided_at,
            evidence=tuple(self._patient_evidence(canonical) for canonical in members),
        )
        self.decisions.append(decision)
        return decision

    def unmerge_patients(
        self,
        group_id: str,
        *,
        reason: str,
        operator: str,
        decided_at: str,
    ) -> IdentityDecision:
        group = self._active_groups().get(group_id)
        if group is None:
            raise ValueError(f"{group_id} is not an active patient merge")
        merge_decision = next(
            item for item in self.decisions if item.group_id == group_id and item.action == "merge"
        )
        decision = IdentityDecision(
            decision_id=len(self.decisions) + 1,
            action="unmerge",
            group_id=group_id,
            members=merge_decision.members,
            reason=reason,
            operator=operator,
            decided_at=decided_at,
            supersedes=merge_decision.decision_id,
        )
        self.decisions.append(decision)
        return decision

    def _require_patient_keys(
        self, patient_keys: list[EntityKey | str]
    ) -> list[str]:
        members: list[str] = []
        for item in patient_keys:
            key = parse_canonical_id(item) if isinstance(item, str) else item
            if key.entity_type != "patient":
                raise ValueError(f"{key.canonical_id()} is not a patient")
            if key not in self.entities:
                raise ValueError(f"unknown patient {key.canonical_id()}")
            members.append(key.canonical_id())
        return members

    def _patient_evidence(self, canonical: str) -> dict[str, Any]:
        key = parse_canonical_id(canonical)
        entity = self.entities[key]
        version = entity.current
        return {
            "patient": canonical,
            "version": version.version,
            "content_hash": version.content_hash,
            "data": version.data,
        }

    def _active_groups(self) -> dict[str, dict[str, Any]]:
        """Replay the identity decision log; unmerge reverses a merge."""
        active: dict[str, dict[str, Any]] = {}
        for decision in self.decisions:
            if decision.action == "merge":
                active[decision.group_id] = {
                    "merge_decision_id": decision.decision_id,
                    "members": set(decision.members),
                }
            else:
                active.pop(decision.group_id, None)
        return active

    def active_patient_groups(self) -> dict[str, list[str]]:
        return {
            group_id: sorted(group["members"]) for group_id, group in self._active_groups().items()
        }

    def _effective_group(self, patient_canonical: str) -> str | None:
        for group_id, group in self._active_groups().items():
            if patient_canonical in group["members"]:
                return group_id
        return None

    def patient_view(self, patient: EntityKey | str) -> dict[str, Any]:
        """Unified patient identity with encounters/observations across sources.

        Passing any member of an active merge returns the merged view. After an
        unmerge each source patient only sees its own original records.
        """
        key = parse_canonical_id(patient) if isinstance(patient, str) else patient
        canonical = key.canonical_id()
        group_id = self._effective_group(canonical)
        if group_id is not None:
            members = set(self._active_groups()[group_id]["members"])
            identity = group_id
        else:
            if key not in self.entities:
                raise ValueError(f"unknown patient {canonical}")
            members = {canonical}
            identity = canonical

        member_keys = {parse_canonical_id(item) for item in members}
        encounters: list[str] = []
        encounter_by_key: dict[EntityKey, EntityKey] = {}
        for entity_key, stored_entity in self.entities.items():
            if entity_key.entity_type != "encounter":
                continue
            target = EntityKey(
                entity_key.source_id, "patient", stored_entity.current.data["patient_ref"]
            )
            if target in member_keys:
                encounters.append(entity_key.canonical_id())
                encounter_by_key[entity_key] = target

        observations: list[str] = []
        for entity_key in self.entities:
            if entity_key.entity_type != "observation":
                continue
            data = self.entities[entity_key].current.data
            patient_target = EntityKey(
                entity_key.source_id, "patient", data["patient_ref"]
            )
            encounter_target = EntityKey(
                entity_key.source_id, "encounter", data["encounter_ref"]
            )
            if patient_target in member_keys or encounter_target in encounter_by_key:
                observations.append(entity_key.canonical_id())

        return {
            "patient": identity,
            "merged": group_id is not None,
            "members": sorted(members),
            "encounters": sorted(encounters),
            "observations": sorted(observations),
        }

    def load_decisions(self, decisions: list[IdentityDecision]) -> None:
        if self.decisions:
            raise ValueError("identity decisions can only be loaded into an empty store")
        self.decisions = list(decisions)


def parse_canonical_id(canonical: str | EntityKey) -> EntityKey:
    if isinstance(canonical, EntityKey):
        return canonical
    source_id, separator, rest = canonical.partition("/")
    entity_type, separator, local_id = rest.partition("/")
    if not separator or not source_id or not entity_type or not local_id:
        raise ValueError(f"invalid canonical entity id {canonical!r}")
    return EntityKey(source_id, entity_type, local_id)


def _content_hash(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def import_bundle(bundle: dict[str, Any], store: RecordStore | None = None) -> tuple[RecordStore, ImportResult]:
    record_store = store or RecordStore()
    return record_store, record_store.import_bundle(bundle)


def import_file(path: str, store: RecordStore | None = None) -> tuple[RecordStore, ImportResult]:
    record_store = store or RecordStore()
    return record_store, record_store.import_file(path)
