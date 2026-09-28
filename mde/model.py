from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


REF_TYPES = ("patient", "encounter", "study")


@dataclass(frozen=True)
class Ref:
    """Local reference inside one source envelope."""

    ref_type: str
    local_id: str

    def key(self) -> tuple[str, str]:
        return (self.ref_type, self.local_id)


@dataclass
class Record:
    type: str
    source_id: str
    local_id: str
    version: int
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.source_id}:{self.type}:{self.local_id}"


@dataclass
class Patient(Record):
    type: str = field(default="patient", init=False)

    @property
    def name(self) -> str:
        return str(self.payload.get("name", ""))


@dataclass
class Encounter(Record):
    type: str = field(default="encounter", init=False)

    @property
    def patient_ref(self) -> Ref:
        return Ref("patient", str(self.payload["patient_ref"]))


@dataclass
class Study(Record):
    type: str = field(default="study", init=False)

    @property
    def encounter_ref(self) -> Ref:
        return Ref("encounter", str(self.payload["encounter_ref"]))

    @property
    def patient_ref(self) -> Ref | None:
        raw = self.payload.get("patient_ref")
        return Ref("patient", str(raw)) if raw is not None else None
