"""The one exchange format supported in this iteration.

mde-json v1: a JSON envelope per source batch::

    {
      "format": "mde-json",
      "format_version": 1,
      "source_id": "hospital-a",
      "batch_id": "b-20260929-01",
      "records": {
        "patient":   [{"id": "p1", "version": 1, "name": "..."}],
        "encounter": [{"id": "e1", "version": 1,
                       "patient_ref": "p1", ...}],
        "study":     [{"id": "s1", "version": 1,
                       "encounter_ref": "e1", "patient_ref": "p1", ...}]
      }
    }

References are local ids within the envelope; they are never resolved by
name. Each source's ids live in an independent namespace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import ValidationError
from .model import Encounter, Patient, Record, Ref, Study

FORMAT_NAME = "mde-json"
FORMAT_VERSION = 1

_ALLOWED_TYPES = {"patient", "encounter", "study"}
_REQUIRED_REFS = {
    "encounter": ("patient_ref",),
    "study": ("encounter_ref",),
}


class Envelope:
    def __init__(self, source_id: str, batch_id: str, records: list[Record]):
        self.source_id = source_id
        self.batch_id = batch_id
        self.records = records

    @property
    def batch_key(self) -> tuple[str, str]:
        return (self.source_id, self.batch_id)


def _require_string(obj: Any, key: str, ctx: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{ctx}: '{key}' must be a non-empty string")
    return value


def _require_int(raw: Any, ctx: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValidationError(f"{ctx}: 'version' must be an integer >= 1")
    return raw


def parse_envelope(raw: Any) -> Envelope:
    if not isinstance(raw, dict):
        raise ValidationError("envelope must be a JSON object")
    if raw.get("format") != FORMAT_NAME:
        raise ValidationError(
            f"unsupported format {raw.get('format')!r}; expected {FORMAT_NAME!r}"
        )
    if raw.get("format_version") != FORMAT_VERSION:
        raise ValidationError(
            f"unsupported format_version {raw.get('format_version')!r}; "
            f"expected {FORMAT_VERSION}"
        )
    source_id = _require_string(raw, "source_id", "envelope")
    batch_id = _require_string(raw, "batch_id", "envelope")

    groups = raw.get("records")
    if not isinstance(groups, dict):
        raise ValidationError("envelope.records must be an object")

    records: list[Record] = []
    seen_local: set[tuple[str, str]] = set()
    for rec_type, items in groups.items():
        if rec_type not in _ALLOWED_TYPES:
            raise ValidationError(f"unknown record type {rec_type!r}")
        if not isinstance(items, list):
            raise ValidationError(f"records.{rec_type} must be a list")
        for index, item in enumerate(items):
            ctx = f"records.{rec_type}[{index}]"
            records.append(_parse_record(rec_type, item, source_id, ctx, seen_local))
    return Envelope(source_id, batch_id, records)


def _parse_record(
    rec_type: str,
    item: Any,
    source_id: str,
    ctx: str,
    seen_local: set[tuple[str, str]],
) -> Record:
    if not isinstance(item, dict):
        raise ValidationError(f"{ctx} must be an object")
    local_id = _require_string(item, "id", ctx)
    version = _require_int(item.get("version"), ctx)
    identity = (rec_type, local_id)
    if identity in seen_local:
        raise ValidationError(f"{ctx}: duplicate id {local_id!r} within envelope")
    seen_local.add(identity)

    for ref_field in _REQUIRED_REFS.get(rec_type, ()):
        ref_value = item.get(ref_field)
        if not isinstance(ref_value, str) or not ref_value:
            raise ValidationError(f"{ctx}: '{ref_field}' must be a non-empty string")
    if rec_type == "study":
        patient_ref = item.get("patient_ref")
        if patient_ref is not None and (
            not isinstance(patient_ref, str) or not patient_ref
        ):
            raise ValidationError(f"{ctx}: 'patient_ref' must be a non-empty string")

    payload = {k: v for k, v in item.items() if k not in {"id", "version"}}
    cls = {"patient": Patient, "encounter": Encounter, "study": Study}[rec_type]
    return cls(source_id, local_id, version, payload)


def parse_text(text: str) -> Envelope:
    try:
        return parse_envelope(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON: {exc}") from exc


def parse_file(path: str | Path) -> Envelope:
    return parse_text(Path(path).read_text(encoding="utf-8"))


def iter_refs(record: Record) -> Iterable[tuple[str, Ref]]:
    if isinstance(record, Encounter):
        yield ("patient_ref", record.patient_ref)
    elif isinstance(record, Study):
        yield ("encounter_ref", record.encounter_ref)
        if record.patient_ref is not None:
            yield ("patient_ref", record.patient_ref)
