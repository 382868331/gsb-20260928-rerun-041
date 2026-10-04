from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EntityType = Literal["patient", "encounter", "observation"]


@dataclass(frozen=True)
class Source:
    id: str
    name: str = ""

    def key(self) -> str:
        return self.id


@dataclass(frozen=True)
class EntityKey:
    source_id: str
    entity_type: str
    source_record_id: str

    def canonical_id(self) -> str:
        return f"{self.source_id}/{self.entity_type}/{self.source_record_id}"


@dataclass(frozen=True)
class Version:
    version: int
    data: dict[str, Any]
    content_hash: str
    recorded_at: str = ""


@dataclass
class Entity:
    key: EntityKey
    current_version: int
    versions: dict[int, Version] = field(default_factory=dict)

    @property
    def current(self) -> Version:
        return self.versions[self.current_version]


@dataclass(frozen=True)
class PendingReference:
    source_entity: str
    field_name: str
    target_type: str
    target: EntityKey


@dataclass(frozen=True)
class IdentityOperation:
    id: str
    operation: str
    timestamp: str
    patients: tuple[EntityKey, ...]
    group_id: str
    canonical_patient: EntityKey | None = None
    reason: str = ""
    operator: str = ""
    previous_groups: dict[str, str] = field(default_factory=dict)
    reverts: str = ""
    patient_versions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RetractionNotice:
    id: str
    source_id: str
    entity_type: str
    record_id: str
    version: int
    timestamp: str
    reason: str = ""
    operator: str = ""

    def entity_key(self) -> EntityKey:
        return EntityKey(self.source_id, self.entity_type, self.record_id)


@dataclass(frozen=True)
class ExportRecord:
    id: str
    timestamp: str
    label: str = ""
    versions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DispositionReplacement:
    entity: str
    from_version: int
    to_version: int
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class DispositionRetention:
    entity: str
    version: int
    restriction: str = ""


@dataclass(frozen=True)
class Disposition:
    id: str
    target_kind: str
    target_id: str
    notice_id: str
    timestamp: str
    operator: str = ""
    reason: str = ""
    source_versions: dict[str, int] = field(default_factory=dict)
    replacements: tuple[DispositionReplacement, ...] = ()
    retained: tuple[DispositionRetention, ...] = ()


@dataclass(frozen=True)
class FieldDecision:
    id: str
    export_id: str
    entity: str
    field: str
    disposition_id: str
    choice: str = ""
    from_version: int = 0
    to_version: int = 0
