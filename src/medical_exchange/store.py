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
from .models import (
    Entity,
    EntityKey,
    IdentityOperation,
    PendingReference,
    ResearchExport,
    Source,
    Version,
    VersionStatusEvent,
    WithdrawalDecision,
    WithdrawalNotice,
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
    withdrawal_notices_accepted: int = 0
    withdrawal_notices_duplicate: int = 0
    withdrawal_notices_pending: int = 0
    withdrawal_notices_verified: int = 0
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
            "withdrawal_notices_accepted": self.withdrawal_notices_accepted,
            "withdrawal_notices_duplicate": self.withdrawal_notices_duplicate,
            "withdrawal_notices_pending": self.withdrawal_notices_pending,
            "withdrawal_notices_verified": self.withdrawal_notices_verified,
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
        self.withdrawal_notices: list[WithdrawalNotice] = []
        self.version_status_events: list[VersionStatusEvent] = []
        self.research_exports: list[ResearchExport] = []
        self.withdrawal_decisions: list[WithdrawalDecision] = []

    def import_bundle(self, bundle: dict[str, Any]) -> ImportResult:
        validate_envelope(bundle)
        if bundle.get("format") == UNIFIED_FORMAT:
            return self._import_unified(bundle)
        source = source_from_bundle(bundle)
        self.sources.setdefault(source.id, source)
        result = ImportResult(source=source.id)
        pending_notices_before = sum(item.status == "pending" for item in self.withdrawal_notices)

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

        for raw_notice in bundle.get("withdrawal_notices", []):
            notice_id = raw_notice.get("id") if isinstance(raw_notice, dict) else None
            try:
                outcome = self._put_withdrawal_notice_raw(raw_notice, source.id)
            except (ValueError, TypeError) as exc:
                result.rejected += 1
                result.failures.append(RecordFailure(notice_id, str(exc)))
                continue
            self._count_withdrawal_outcome(outcome, result)

        resolved, pending = self._rebuild_references(source.id)
        self._replay_identity_groups()
        pending_notices_after = sum(item.status == "pending" for item in self.withdrawal_notices)
        newly_verified_notices = pending_notices_before - pending_notices_after
        result.withdrawal_notices_verified += max(0, newly_verified_notices)
        result.withdrawal_notices_accepted += max(0, newly_verified_notices)
        result.resolved_references = resolved
        result.pending_references = pending
        result.manual_decisions_preserved = self._active_merge_count()
        return result

    def import_file(self, path: str) -> ImportResult:
        return self.import_bundle(parse_file(path))

    def receive_withdrawal_notice(
        self,
        *,
        source_id: str,
        record_id: str,
        record_type: str,
        version: int,
        notice_id: str | None = None,
        reason: str = "",
        operator: str = "",
        received_at: str | None = None,
        timestamp: str | None = None,
    ) -> tuple[str, WithdrawalNotice]:
        raw = {
            "id": notice_id or f"withdrawal/{source_id}/{record_type}/{record_id}/v{version}",
            "record_id": record_id,
            "type": record_type,
            "version": version,
            "reason": reason,
            "operator": operator,
            "received_at": received_at or timestamp or _utc_now(),
        }
        return self._put_withdrawal_notice_raw(raw, source_id)

    def _put_withdrawal_notice_raw(self, raw: Any, envelope_source_id: str | None = None) -> tuple[str, WithdrawalNotice]:
        if not isinstance(raw, dict):
            raise ValidationError("withdrawal notice must be an object")
        notice_id = raw.get("id")
        if notice_id is None:
            notice_id = None
        elif not isinstance(notice_id, str) or not notice_id.strip():
            raise ValidationError("withdrawal notice id must be a non-empty string")

        if envelope_source_id is not None:
            source_id = envelope_source_id
            raw_source = raw.get("source_id")
            source_value = raw.get("source")
            if isinstance(source_value, dict):
                raw_source = raw_source or source_value.get("id")
            if raw_source not in (None, "", source_id):
                raise ConflictError("withdrawal notice source does not match its bundle source")
        else:
            source_id = raw.get("source_id")
            if not isinstance(source_id, str) or not source_id.strip():
                target = raw.get("target_entity")
                if not isinstance(target, str):
                    raise ValidationError("withdrawal notice source_id is required")
                source_id = self._entity_key_from_id(target).source_id

        if isinstance(raw.get("target_entity"), str):
            key = self._entity_key_from_id(raw["target_entity"])
            if key.source_id != source_id:
                raise ConflictError("withdrawal notice target does not match its source")
            record_type = key.entity_type
            record_id = key.source_record_id
        else:
            record_id = raw.get("record_id") or raw.get("source_record_id")
            record_type = raw.get("type") or raw.get("entity_type")
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValidationError("withdrawal notice record_id is required")
        if record_type not in ("patient", "encounter", "observation"):
            raise ValidationError("withdrawal notice type must be a known entity type")
        notice_version = raw.get("version")
        if not isinstance(notice_version, int) or isinstance(notice_version, bool) or notice_version < 1:
            raise ValidationError("withdrawal notice version must be a positive integer")
        event_time = raw.get("received_at") or raw.get("timestamp") or _utc_now()
        if not isinstance(event_time, str) or not event_time.strip():
            raise ValidationError("withdrawal notice received_at must be a string")
        notice_id = notice_id or f"withdrawal/{source_id}/{record_type}/{record_id}/v{notice_version}"

        duplicate = next((item for item in self.withdrawal_notices if item.id == notice_id), None)
        if duplicate is not None:
            candidate_fields = (duplicate.source_id, duplicate.entity_type, duplicate.source_record_id, duplicate.version)
            if candidate_fields != (source_id, record_type, record_id, notice_version):
                raise ConflictError(f"withdrawal notice {notice_id!r} points to a different source version")
            return "duplicate", duplicate

        same_target = next(
            (
                item
                for item in self.withdrawal_notices
                if (item.source_id, item.entity_type, item.source_record_id, item.version)
                == (source_id, record_type, record_id, notice_version)
            ),
            None,
        )
        if same_target is not None:
            return "duplicate", same_target

        notice = WithdrawalNotice(
            id=notice_id,
            source_id=source_id,
            entity_type=record_type,
            source_record_id=record_id,
            version=notice_version,
            received_at=event_time,
            reason=str(raw.get("reason", "") or ""),
            operator=str(raw.get("operator", "") or ""),
            status="pending",
            verified_at=str(raw.get("verified_at", "") or ""),
        )
        self.withdrawal_notices.append(notice)
        self.sources.setdefault(source_id, Source(source_id, ""))
        outcome, notice = self._verify_withdrawal_notice(notice)
        return outcome, notice

    def _verify_withdrawal_notice(self, notice: WithdrawalNotice) -> tuple[str, WithdrawalNotice]:
        if any(event.notice_id == notice.id for event in self.version_status_events):
            verified = self._replace_notice(notice, status="verified", verified_at=notice.verified_at)
            return "duplicate", verified
        key = notice.key
        entity = self.entities.get(key)
        if entity is None or notice.version not in entity.versions:
            return "pending", notice
        event = VersionStatusEvent(
            id=f"status/{notice.id}",
            notice_id=notice.id,
            source_id=notice.source_id,
            entity_type=notice.entity_type,
            source_record_id=notice.source_record_id,
            version=notice.version,
            event="withdrawn",
            timestamp=notice.received_at,
            reason=notice.reason,
            operator=notice.operator,
        )
        self.version_status_events.append(event)
        verified = self._replace_notice(
            notice,
            status="verified",
            verified_at=notice.verified_at or notice.received_at,
        )
        return "accepted", verified

    def _replace_notice(self, notice: WithdrawalNotice, **changes: Any) -> WithdrawalNotice:
        updated = WithdrawalNotice(
            **{**notice.__dict__, **changes}
        )
        index = self.withdrawal_notices.index(notice)
        self.withdrawal_notices[index] = updated
        return updated

    def _sync_withdrawal_notices(self) -> tuple[int, int, int, int]:
        accepted = duplicate = pending = verified = 0
        for notice in list(self.withdrawal_notices):
            if notice.status == "verified":
                verified += 1
                continue
            outcome, updated = self._verify_withdrawal_notice(notice)
            if outcome == "accepted":
                accepted += 1
                verified += 1
            elif outcome == "duplicate":
                duplicate += 1
                verified += 1
            else:
                pending += 1
        return accepted, duplicate, pending, verified

    def _sync_withdrawal_notices_for_key(self, key: EntityKey) -> None:
        for notice in list(self.withdrawal_notices):
            if notice.status != "pending" or notice.key != key:
                continue
            self._verify_withdrawal_notice(notice)

    @staticmethod
    def _count_withdrawal_outcome(outcome: str, result: ImportResult) -> None:
        if outcome == "accepted":
            result.withdrawal_notices_accepted += 1
            result.withdrawal_notices_verified += 1
        elif outcome == "duplicate":
            result.withdrawal_notices_duplicate += 1
            result.withdrawal_notices_verified += 1
        elif outcome == "pending":
            result.withdrawal_notices_accepted += 1
            result.withdrawal_notices_pending += 1

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
        patient_versions = {}
        for key in (first, second):
            effective_version = self.effective_current_version(key)
            patient_versions[key.canonical_id()] = (
                effective_version
                if effective_version is not None
                else self.entities[key].current_version
            )
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
        result = self.current_valid_records(patient)
        return {
            "patient": result["patient"],
            "encounters": result["encounters"],
            "observations": result["observations"],
            "withdrawn": result["withdrawn"],
            "restrictions": result["restrictions"],
        }

    def current_valid_records(self, patient: str | EntityKey) -> dict[str, Any]:
        canonical = self.canonical_patient(patient)
        group = set(self.patient_group(canonical))
        encounters = []
        observations = []
        encounter_patients: dict[EntityKey, EntityKey] = {}
        withdrawn = []
        restrictions = []

        for key, entity in self.entities.items():
            if key.entity_type != "encounter":
                continue
            effective_version_number = self.effective_current_version(key)
            if effective_version_number is None:
                withdrawn.append({"entity": key.canonical_id(), "version": self.effective_current_version(key), "reason": "withdrawn"})
                continue
            effective_entity = self._entity_at_version(entity, effective_version_number)
            patient_ref = EntityKey(
                key.source_id, "patient", effective_entity.current.data["patient_ref"]
            )
            if patient_ref not in self.entities or self.effective_current_version(patient_ref) is None:
                withdrawn.append({"entity": key.canonical_id(), "version": effective_version_number, "reason": "unavailable reference"})
                continue
            patient_key = patient_ref
            resolved = self.canonical_patient(patient_key) if patient_key in self.patient_groups else patient_key
            encounter_patients[key] = resolved
            if resolved == canonical or patient_key in group:
                encounters.append(effective_entity)
                if entity.current_version != effective_version_number:
                    restrictions.append(
                        f"{key.canonical_id()} current version {entity.current_version} is withdrawn; using version {effective_version_number}"
                    )

        for key, entity in self.entities.items():
            if key.entity_type != "observation":
                continue
            effective_version_number = self.effective_current_version(key)
            if effective_version_number is None:
                withdrawn.append({"entity": key.canonical_id(), "version": self.effective_current_version(key), "reason": "withdrawn"})
                continue
            effective_entity = self._entity_at_version(entity, effective_version_number)
            patient_key = EntityKey(key.source_id, "patient", effective_entity.current.data["patient_ref"])
            encounter_key = EntityKey(key.source_id, "encounter", effective_entity.current.data["encounter_ref"])
            if (
                patient_key not in self.entities
                or encounter_key not in self.entities
                or self.effective_current_version(patient_key) is None
                or self.effective_current_version(encounter_key) is None
            ):
                withdrawn.append({"entity": key.canonical_id(), "version": effective_version_number, "reason": "unavailable reference"})
                continue
            patient_matches = patient_key in group or (
                patient_key in self.patient_groups and self.canonical_patient(patient_key) == canonical
            )
            encounter_matches = encounter_patients.get(encounter_key) == canonical
            if patient_matches or encounter_matches:
                observations.append(effective_entity)
                if entity.current_version != effective_version_number:
                    restrictions.append(
                        f"{key.canonical_id()} current version {entity.current_version} is withdrawn; using version {effective_version_number}"
                    )

        patient_effective_version = self.effective_current_version(canonical)
        patient_has_effective_version = patient_effective_version is not None
        if not patient_has_effective_version:
            withdrawn.append({
                "entity": canonical.canonical_id(),
                "version": self.entities[canonical].current_version,
                "reason": "withdrawn",
            })
        elif self.entities[canonical].current_version != patient_effective_version:
            restrictions.append(
                f"{canonical.canonical_id()} current version {self.entities[canonical].current_version} is withdrawn; using version {patient_effective_version}"
            )
        return {
            "patient": [self._entity_at_version(self.entities[canonical], patient_effective_version)] if patient_has_effective_version else [],
            "encounters": sorted(encounters, key=lambda item: item.key.canonical_id()),
            "observations": sorted(observations, key=lambda item: item.key.canonical_id()),
            "withdrawn": sorted(withdrawn, key=lambda item: item["entity"]),
            "restrictions": sorted(restrictions) + [
                f"{item['entity']} version {item['version']} is withdrawn and is not valid current material"
                for item in sorted(withdrawn, key=lambda item: item["entity"])
            ],
        }

    def visible_history(self, patient: str | EntityKey) -> dict[str, Any]:
        key = self._patient_key(patient)
        self._require_existing_patient(key)
        records = self.patient_records(key)
        entity_keys = set(self.patient_group(key))
        entity_keys.update(item.key for item in records["encounters"])
        entity_keys.update(item.key for item in records["observations"])
        for entity_key, entity in self.entities.items():
            if entity_key.entity_type not in ("encounter", "observation"):
                continue
            data = entity.current.data
            patient_ref = EntityKey(entity_key.source_id, "patient", data.get("patient_ref", ""))
            if patient_ref in entity_keys:
                entity_keys.add(entity_key)
            if entity_key.entity_type == "observation":
                encounter_ref = EntityKey(entity_key.source_id, "encounter", data.get("encounter_ref", ""))
                if encounter_ref in entity_keys:
                    entity_keys.add(entity_key)
        return {
            "records": [self.entity_history(item) for item in entity_keys],
            "withdrawn_notices": [
                self._withdrawal_notice_dict(item)
                for item in self.withdrawal_notices
                if item.key in set(entity_keys)
            ],
        }

    def entity_history(self, key: str | EntityKey) -> dict[str, Any]:
        entity_key = self._entity_key_from_id(key) if isinstance(key, str) else key
        entity = self.entities[entity_key]
        return {
            "entity": entity_key.canonical_id(),
            "current_version": entity.current_version,
            "effective_current_version": self.effective_current_version(entity_key),
            "versions": [
                {
                    "version": item.version,
                    "data": item.data,
                    "content_hash": item.content_hash,
                    "status": "withdrawn" if self.is_version_withdrawn(entity_key, item.version) else "active",
                }
                for item in sorted(entity.versions.values(), key=lambda version: version.version)
            ],
        }

    def version_timeline(self, key: str | EntityKey) -> list[dict[str, Any]]:
        entity_key = self._entity_key_from_id(key) if isinstance(key, str) else key
        return [
            {
                "id": item.id,
                "notice_id": item.notice_id,
                "target_entity": item.key.canonical_id(),
                "version": item.version,
                "event": item.event,
                "timestamp": item.timestamp,
                "reason": item.reason,
                "operator": item.operator,
            }
            for item in sorted(
                (
                    event
                    for event in self.version_status_events
                    if event.key == entity_key
                ),
                key=lambda event: (event.timestamp, event.id),
            )
        ]

    def create_research_export(
        self,
        selected_versions: dict[str | EntityKey, int],
        *,
        export_id: str | None = None,
        description: str = "",
        operator: str = "",
        timestamp: str | None = None,
    ) -> ResearchExport:
        resolved = {
            (key.canonical_id() if isinstance(key, EntityKey) else key): int(version)
            for key, version in selected_versions.items()
        }
        for entity_id, version in resolved.items():
            key = self._entity_key_from_id(entity_id)
            entity = self.entities.get(key)
            if entity is None or version not in entity.versions:
                raise ValueError(f"research export references missing version {entity_id} v{version}")
            if self.is_version_withdrawn(key, version):
                raise ConflictError(f"research export cannot use withdrawn version {entity_id} v{version}")
        event_time = timestamp or _utc_now()
        export_id = export_id or _operation_id("research-export", event_time, len(self.research_exports))
        if any(item.id == export_id for item in self.research_exports):
            raise ConflictError(f"research export {export_id!r} already exists")
        item = ResearchExport(
            id=export_id,
            timestamp=event_time,
            selected_versions=resolved,
            description=description,
            operator=operator,
        )
        self.research_exports.append(item)
        return item

    def withdrawal_impacts(self, *, include_resolved: bool = False) -> list[dict[str, Any]]:
        impacts = []
        withdrawn = {
            (event.key.canonical_id(), event.version): event
            for event in self.version_status_events
            if event.event == "withdrawn"
        }
        for operation in self.identity_operations:
            if operation.operation != "merge" or self._merge_is_reverted(operation.id):
                continue
            for entity_id, version in operation.patient_versions.items():
                event = withdrawn.get((entity_id, version))
                if event is None:
                    continue
                context = f"merge/{operation.id}"
                decision = self._decision_for(context, event.notice_id)
                if decision is None or include_resolved:
                    impacts.append(self._impact_dict("manual_merge", context, operation.id, event, decision))
        for exported in self.research_exports:
            for entity_id, version in exported.selected_versions.items():
                event = withdrawn.get((entity_id, version))
                if event is None:
                    continue
                context = f"research_export/{exported.id}"
                decision = self._decision_for(context, event.notice_id)
                if decision is None or include_resolved:
                    impacts.append(self._impact_dict("research_export", context, exported.id, event, decision))
        return sorted(impacts, key=lambda item: (item["notice_id"], item["affected_context"], item["entity"]))

    def resolve_withdrawal_impact(
        self,
        affected_context: str,
        notice_id: str,
        action: str,
        *,
        reason: str = "",
        operator: str = "",
        timestamp: str | None = None,
        decision_id: str | None = None,
        replacement_versions: dict[str, int] | None = None,
    ) -> WithdrawalDecision:
        if action not in ("reselect", "retain_limited_history"):
            raise ValueError("action must be 'reselect' or 'retain_limited_history'")
        if not any(item.id == notice_id and item.status == "verified" for item in self.withdrawal_notices):
            raise ValueError("verified withdrawal notice is required")
        if not any(item["affected_context"] == affected_context for item in self.withdrawal_impacts(include_resolved=True)):
            raise ValueError("affected context does not depend on the withdrawn version")
        if any(item.affected_context == affected_context and item.notice_id == notice_id for item in self.withdrawal_decisions):
            return next(
                item
                for item in self.withdrawal_decisions
                if item.affected_context == affected_context and item.notice_id == notice_id
            )
        event_time = timestamp or _utc_now()
        decision_id = decision_id or _operation_id("withdrawal-decision", event_time, len(self.withdrawal_decisions))
        decision = WithdrawalDecision(
            id=decision_id,
            notice_id=notice_id,
            affected_context=affected_context,
            action=action,
            timestamp=event_time,
            reason=reason,
            operator=operator,
        )
        self.withdrawal_decisions.append(decision)
        if action == "reselect" and affected_context.startswith("research_export/"):
            export_id = affected_context.split("/", 1)[1]
            index = next(i for i, item in enumerate(self.research_exports) if item.id == export_id)
            old = self.research_exports[index]
            selected = dict(old.selected_versions)
            for entity_id, new_version in (replacement_versions or {}).items():
                new_key = self._entity_key_from_id(entity_id)
                if new_key not in self.entities or int(new_version) not in self.entities[new_key].versions:
                    raise ValueError(f"replacement version {entity_id} v{new_version} does not exist")
                if self.is_version_withdrawn(new_key, int(new_version)):
                    raise ConflictError(f"replacement version {entity_id} v{new_version} is withdrawn")
                selected[entity_id] = int(new_version)
            self.research_exports[index] = ResearchExport(
                id=old.id,
                timestamp=old.timestamp,
                selected_versions=selected,
                description=old.description,
                operator=old.operator,
                restrictions=tuple(old.restrictions) + (f"reselected after withdrawal notice {notice_id}",),
            )
        elif action == "retain_limited_history":
            if affected_context.startswith("research_export/"):
                export_id = affected_context.split("/", 1)[1]
                index = next(i for i, item in enumerate(self.research_exports) if item.id == export_id)
                old = self.research_exports[index]
                self.research_exports[index] = ResearchExport(
                    id=old.id,
                    timestamp=old.timestamp,
                    selected_versions=dict(old.selected_versions),
                    description=old.description,
                    operator=old.operator,
                    restrictions=tuple(old.restrictions) + (f"retained limited historical result due to {notice_id}",),
                )
        return decision

    def is_version_withdrawn(self, key: EntityKey, version: int) -> bool:
        return any(
            event.key == key and event.version == version and event.event == "withdrawn"
            for event in self.version_status_events
        )

    def withdrawn_versions(self, key: str | EntityKey) -> set[int]:
        entity_key = self._entity_key_from_id(key) if isinstance(key, str) else key
        return {
            event.version
            for event in self.version_status_events
            if event.key == entity_key and event.event == "withdrawn"
        }

    def effective_current_version(self, key: str | EntityKey) -> int | None:
        entity_key = self._entity_key_from_id(key) if isinstance(key, str) else key
        entity = self.entities[entity_key]
        active = [version for version in entity.versions if not self.is_version_withdrawn(entity_key, version)]
        return max(active) if active else None

    def effective_version(self, entity: Entity) -> Version:
        version = self.effective_current_version(entity.key)
        if version is None:
            raise ConflictError(f"{entity.key.canonical_id()} has no effective non-withdrawn version")
        return entity.versions[version]

    def _entity_at_version(self, entity: Entity, version: int) -> Entity:
        return Entity(key=entity.key, current_version=version, versions=entity.versions)

    def _has_effective_version(self, entity: Entity) -> bool:
        return self.effective_current_version(entity.key) is not None

    def _merge_is_reverted(self, merge_id: str) -> bool:
        return any(item.operation == "revert_merge" and item.reverts == merge_id for item in self.identity_operations)

    def _decision_for(self, context: str, notice_id: str) -> WithdrawalDecision | None:
        return next(
            (
                item
                for item in self.withdrawal_decisions
                if item.affected_context == context and item.notice_id == notice_id
            ),
            None,
        )

    def _impact_dict(self, dependency_type: str, context: str, dependency_id: str, event: VersionStatusEvent, decision: WithdrawalDecision | None) -> dict[str, Any]:
        return {
            "dependency_type": dependency_type,
            "affected_context": context,
            "affected_record": dependency_id,
            "notice_id": event.notice_id,
            "entity": event.key.canonical_id(),
            "version": event.version,
            "status": "resolved" if decision is not None else "awaiting_decision",
            "decision": self._decision_dict(decision) if decision is not None else None,
        }

    @staticmethod
    def _decision_dict(decision: WithdrawalDecision) -> dict[str, Any]:
        return {
            "id": decision.id,
            "action": decision.action,
            "timestamp": decision.timestamp,
            "reason": decision.reason,
            "operator": decision.operator,
        }

    @staticmethod
    def _withdrawal_notice_dict(notice: WithdrawalNotice) -> dict[str, Any]:
        return {
            "id": notice.id,
            "target_entity": notice.target_entity,
            "version": notice.version,
            "received_at": notice.received_at,
            "status": notice.status,
            "verified_at": notice.verified_at,
            "reason": notice.reason,
            "operator": notice.operator,
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
            self._sync_withdrawal_notices_for_key(key)
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
        self._sync_withdrawal_notices_for_key(key)
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
            self._sync_withdrawal_notices_for_key(key)
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
        self._sync_withdrawal_notices_for_key(key)
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
            effective_version_number = self.effective_current_version(key)
            if effective_version_number is None:
                continue
            effective_entity = self._entity_at_version(entity, effective_version_number)
            for field_name, target_type in REFERENCE_FIELDS.get(key.entity_type, ()):
                target_local_id = effective_entity.current.data[field_name]
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
                outcomes = []
                if isinstance(item.get("data"), dict):
                    outcomes.append(self._restore_unified_entity_version(key, item))
                for historical in item.get("versions", []):
                    if not isinstance(historical, dict):
                        raise ValueError("entity version entries must be objects")
                    if "data" not in historical:
                        continue
                    if isinstance(item.get("data"), dict) and int(historical["version"]) == current_version:
                        continue
                    outcomes.append(self._restore_unified_entity_version(key, historical))
                if not outcomes:
                    raise ValueError("entity version data must be an object")
            except (KeyError, TypeError, ValueError) as exc:
                result.rejected += 1
                result.failures.append(RecordFailure(entity_id, str(exc)))
                continue
            result.accepted += sum(outcome == "accepted" for outcome in outcomes)
            result.duplicate += sum(outcome == "duplicate" for outcome in outcomes)

        notice_outcomes = {"accepted": 0, "duplicate": 0, "pending": 0}
        for raw_notice in bundle.get("withdrawal_notices", []):
            notice_id = raw_notice.get("id") if isinstance(raw_notice, dict) else None
            try:
                outcome, _ = self._put_withdrawal_notice_raw(raw_notice)
            except (ValueError, TypeError) as exc:
                result.rejected += 1
                result.failures.append(RecordFailure(notice_id, str(exc)))
                continue
            notice_outcomes[outcome] += 1
        for raw_event in bundle.get("version_status_events", []):
            self._import_version_status_event(raw_event)
        accepted_from_pending, duplicate_from_events, still_pending, _ = self._sync_withdrawal_notices()
        for raw_export in bundle.get("research_exports", []):
            self._import_research_export(raw_export)
        for raw_decision in bundle.get("withdrawal_decisions", []):
            self._import_withdrawal_decision(raw_decision)

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
        result.withdrawal_notices_accepted = notice_outcomes["accepted"] + still_pending + accepted_from_pending
        result.withdrawal_notices_duplicate = notice_outcomes["duplicate"] + duplicate_from_events
        result.withdrawal_notices_pending = still_pending
        result.withdrawal_notices_verified = notice_outcomes["accepted"] + notice_outcomes["duplicate"] + accepted_from_pending + duplicate_from_events
        return result

    def _import_version_status_event(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        event_id = raw.get("id")
        if isinstance(event_id, str) and any(item.id == event_id for item in self.version_status_events):
            return
        target = raw.get("target_entity")
        if not isinstance(target, str):
            source_id = raw.get("source_id", "")
            target = f"{source_id}/{raw.get('entity_type')}/{raw.get('source_record_id')}"
        key = self._entity_key_from_id(target)
        version = int(raw["version"])
        if key not in self.entities or version not in self.entities[key].versions:
            return
        notice_id = str(raw.get("notice_id", ""))
        if any(event.notice_id == notice_id and event.key == key and event.version == version for event in self.version_status_events):
            return
        self.version_status_events.append(
            VersionStatusEvent(
                id=str(event_id or f"status/{notice_id}"),
                notice_id=notice_id,
                source_id=key.source_id,
                entity_type=key.entity_type,
                source_record_id=key.source_record_id,
                version=version,
                event=str(raw.get("event", "withdrawn")),
                timestamp=str(raw.get("timestamp", "")),
                reason=str(raw.get("reason", "") or ""),
                operator=str(raw.get("operator", "") or ""),
            )
        )
        index = next((i for i, item in enumerate(self.withdrawal_notices) if item.id == notice_id), None)
        if index is not None and self.withdrawal_notices[index].status != "verified":
            notice = self.withdrawal_notices[index]
            self.withdrawal_notices[index] = WithdrawalNotice(
                **{
                    **notice.__dict__,
                    "status": "verified",
                    "verified_at": notice.verified_at or str(raw.get("timestamp", "") or ""),
                }
            )

    def _import_research_export(self, raw: Any) -> None:
        if not isinstance(raw, dict) or any(item.id == raw.get("id") for item in self.research_exports):
            return
        self.research_exports.append(
            ResearchExport(
                id=str(raw.get("id")),
                timestamp=str(raw.get("timestamp", "")),
                selected_versions={str(key): int(version) for key, version in raw.get("selected_versions", {}).items()},
                description=str(raw.get("description", "") or ""),
                operator=str(raw.get("operator", "") or ""),
                restrictions=tuple(raw.get("restrictions", ())),
            )
        )

    def _import_withdrawal_decision(self, raw: Any) -> None:
        if not isinstance(raw, dict) or any(item.id == raw.get("id") for item in self.withdrawal_decisions):
            return
        self.withdrawal_decisions.append(
            WithdrawalDecision(
                id=str(raw.get("id")),
                notice_id=str(raw.get("notice_id", "")),
                affected_context=str(raw.get("affected_context", "")),
                action=str(raw.get("action", "")),
                timestamp=str(raw.get("timestamp", "")),
                reason=str(raw.get("reason", "") or ""),
                operator=str(raw.get("operator", "") or ""),
            )
        )

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
