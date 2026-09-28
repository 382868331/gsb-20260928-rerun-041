import json

import pytest

from mde import ConflictError, RecordStore, ValidationError
from mde.format import parse_text


def envelope(records, source_id="hosp-a", batch_id="b1"):
    return {
        "format": "mde-json",
        "format_version": 1,
        "source_id": source_id,
        "batch_id": batch_id,
        "records": records,
    }


PATIENT = {
    "patient": [{"id": "p1", "version": 1, "name": "张伟", "gender": "M"}],
}
ENCOUNTER = {
    "encounter": [
        {"id": "e1", "version": 1, "patient_ref": "p1", "date": "2026-09-28"}
    ],
}
STUDY = {
    "study": [
        {
            "id": "s1",
            "version": 1,
            "encounter_ref": "e1",
            "patient_ref": "p1",
            "modality": "CT",
        }
    ],
}


def test_in_order_import_resolves_chain():
    store = RecordStore()
    assert store.import_envelope(envelope(PATIENT, batch_id="p")).ok
    assert store.import_envelope(envelope(ENCOUNTER, batch_id="e")).ok
    assert store.import_envelope(envelope(STUDY, batch_id="s")).ok

    unified = store.export_unified()
    assert len(unified["patients"]) == 1
    assert len(unified["encounters"]) == 1
    assert len(unified["studies"]) == 1
    study = unified["studies"][0]
    assert study["links"]["encounter_ref"] == "hosp-a:encounter:e1"
    assert study["links"]["patient_ref"] == "hosp-a:patient:p1"
    assert unified["pending_relations"] == []


def test_out_of_order_keeps_pending_then_resolves():
    store = RecordStore()
    first = store.import_envelope(envelope(STUDY, batch_id="s"))
    assert first.ok
    assert first.imported == ["hosp-a:study:s1"]

    pending = store.export_unified()["pending_relations"]
    assert {
        "owner": "hosp-a:study:s1",
        "field": "encounter_ref",
        "missing": "hosp-a:encounter:e1",
    } in pending
    assert {
        "owner": "hosp-a:study:s1",
        "field": "patient_ref",
        "missing": "hosp-a:patient:p1",
    } in pending

    # A same-name patient arriving under a different local id must not satisfy
    # the pending relation.
    stranger = {"patient": [{"id": "pX", "version": 1, "name": "张伟"}]}
    store.import_envelope(envelope(stranger, batch_id="stranger"))
    assert len(store.export_unified()["pending_relations"]) == 2

    # Correct targets resolve the parked relations.
    store.import_envelope(envelope(PATIENT, batch_id="p"))
    store.import_envelope(envelope(ENCOUNTER, batch_id="e"))
    assert store.export_unified()["pending_relations"] == []


def test_update_same_identity_never_duplicates_patient():
    store = RecordStore()
    store.import_envelope(envelope(PATIENT, batch_id="v1"))
    updated = {
        "patient": [
            {"id": "p1", "version": 2, "name": "张伟", "gender": "M", "age": 42}
        ]
    }
    result = store.import_envelope(envelope(updated, batch_id="v2"))
    assert result.updated == ["hosp-a:patient:p1"]
    patients = store.export_unified()["patients"]
    assert len(patients) == 1
    assert patients[0]["version"] == 2
    assert patients[0]["data"]["age"] == 42


def test_stale_and_duplicate_delivery_are_separate_outcomes():
    store = RecordStore()
    store.import_envelope(envelope(PATIENT, batch_id="b"))
    again = store.import_envelope(envelope(PATIENT, batch_id="b"))
    assert again.duplicate_batch is True
    assert again.imported == []
    assert len(store.export_unified()["patients"]) == 1

    store.import_envelope(
        envelope(
            {"patient": [{"id": "p1", "version": 2, "name": "张伟", "gender": "M"}]},
            batch_id="v2",
        )
    )
    old_result = store.import_envelope(envelope(PATIENT, batch_id="old-replay"))
    assert old_result.stale == ["hosp-a:patient:p1"]

    same_version_different_payload = envelope(
        {"patient": [{"id": "p1", "version": 2, "name": "王芳", "gender": "M"}]},
        batch_id="clash",
    )
    clash = store.import_envelope(same_version_different_payload)
    assert not clash.ok
    assert "conflicting" in clash.failure.reason


def test_validation_failure_is_a_clear_boundary():
    store = RecordStore()
    bad = {
        "patient": [{"id": "p1", "version": 1, "name": "张伟"}],
        "encounter": [{"id": "e1", "version": 1, "date": "x"}],  # missing ref
    }
    result = store.import_envelope(envelope(bad, batch_id="bad"))
    assert not result.ok
    # Nothing from the malformed batch is applied.
    assert store.export_unified()["patients"] == []

    unknown_format = {"format": "hl7", "format_version": 1}
    assert not store.import_envelope(unknown_format).ok


def test_source_namespaces_are_isolated():
    store = RecordStore()
    store.import_envelope(envelope(PATIENT, source_id="a", batch_id="b"))
    other = {"patient": [{"id": "p1", "version": 1, "name": "张伟"}]}
    store.import_envelope(envelope(other, source_id="b", batch_id="b"))
    unified = store.export_unified()
    assert {p["uid"] for p in unified["patients"]} == {
        "a:patient:p1",
        "b:patient:p1",
    }


def test_round_trip_export_restores_relationships(tmp_path):
    store = RecordStore()
    # Deliver entirely out of order in separate batches.
    store.import_envelope(envelope(STUDY, batch_id="1"))
    store.import_envelope(envelope(PATIENT, batch_id="2"))
    store.import_envelope(envelope(ENCOUNTER, batch_id="3"))

    exported = store.export_source_envelope("hosp-a", batch_id="roundtrip")
    path = tmp_path / "export.json"
    path.write_text(json.dumps(exported, ensure_ascii=False), encoding="utf-8")

    fresh = RecordStore()
    result = fresh.import_file(str(path))
    assert result.ok and result.duplicates == []
    unified = fresh.export_unified()
    links = unified["studies"][0]["links"]
    assert links == {
        "encounter_ref": "hosp-a:encounter:e1",
        "patient_ref": "hosp-a:patient:p1",
    }


def test_unresolved_study_is_kept_but_excluded_from_roundtrip():
    store = RecordStore()
    store.import_envelope(envelope(STUDY, batch_id="s"))
    assert len(store.export_unified()["studies"]) == 1
    exported = store.export_source_envelope("hosp-a")
    # mde-json requires every reference to be satisfiable; only resolved rows
    # are emitted, while the unified export still shows the pending relation.
    assert exported["records"]["study"] == []


def test_parser_rejects_bad_documents():
    with pytest.raises(ValidationError):
        parse_text("{not json")
    with pytest.raises(ValidationError):
        parse_text(json.dumps({"format": "mde-json", "format_version": 2}))
    duplicate = envelope(
        {
            "patient": [
                {"id": "p1", "version": 1},
                {"id": "p1", "version": 2},
            ]
        }
    )
    assert not RecordStore().import_envelope(duplicate).ok
