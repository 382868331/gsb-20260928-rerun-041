from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .exceptions import ImportFormatError, ValidationError
from .models import Source

FORMAT = "medlocal.v1"
ENTITY_TYPES = {"patient", "encounter", "observation"}


def parse_file(path: str | Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ImportFormatError(f"cannot read exchange file: {exc}") from exc
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImportFormatError(f"invalid JSON: {exc.msg}") from exc
    validate_envelope(bundle)
    return bundle


def validate_envelope(bundle: Any) -> None:
    if not isinstance(bundle, dict):
        raise ImportFormatError("exchange bundle must be an object")
    if bundle.get("format") != FORMAT:
        raise ImportFormatError(f"unsupported format; expected {FORMAT!r}")
    source = bundle.get("source")
    if not isinstance(source, dict) or not _nonempty_string(source.get("id")):
        raise ImportFormatError("source.id is required and must be a non-empty string")
    records = bundle.get("records")
    if not isinstance(records, list):
        raise ImportFormatError("records must be a list")


def source_from_bundle(bundle: dict[str, Any]) -> Source:
    source = bundle["source"]
    name = source.get("name", "")
    if name is not None and not isinstance(name, str):
        raise ImportFormatError("source.name must be a string when present")
    return Source(id=source["id"], name=name or "")


def validate_record(record: Any) -> tuple[str, str, int, dict[str, Any]]:
    if not isinstance(record, dict):
        raise ValidationError("record must be an object")
    record_id = record.get("id")
    entity_type = record.get("type")
    version = record.get("version")
    data = record.get("data")

    if not _nonempty_string(record_id):
        raise ValidationError("record id is required and must be a non-empty string")
    if entity_type not in ENTITY_TYPES:
        raise ValidationError(f"record type must be one of {sorted(ENTITY_TYPES)}")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValidationError("record version must be a positive integer")
    if not isinstance(data, dict):
        raise ValidationError("record data must be an object")

    if entity_type == "patient":
        _require_name(data)
    elif entity_type == "encounter":
        _require_local_ref(data, "patient_ref")
    else:
        _require_local_ref(data, "patient_ref")
        _require_local_ref(data, "encounter_ref")
        if not _nonempty_string(data.get("code")):
            raise ValidationError("observation data.code is required")

    return record_id, entity_type, version, data


def _require_name(data: dict[str, Any]) -> None:
    if not _nonempty_string(data.get("name")):
        raise ValidationError("patient data.name is required")


def _require_local_ref(data: dict[str, Any], field_name: str) -> None:
    value = data.get(field_name)
    if not _nonempty_string(value):
        raise ValidationError(f"{field_name} must be a non-empty local record id")


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

