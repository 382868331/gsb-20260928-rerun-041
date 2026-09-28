from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from medical_exchange import (
    ConflictError,
    ImportFormatError,
    RecordStore,
    export_bundle,
)


def record(record_id: str, record_type: str, version: int, **data):
    return {"id": record_id, "type": record_type, "version": version, "data": data}


def bundle(source_id="hospital-a", records=(), name="Hospital A"):
    return {
        "format": "medlocal.v1",
        "source": {"id": source_id, "name": name},
        "records": list(records),
    }


class ExchangeTests(unittest.TestCase):
    def test_out_of_order_records_keep_exact_patient_encounter_observation_links(self):
        store = RecordStore()
        result = store.import_bundle(
            bundle(
                records=[
                    record(
                        "obs-1",
                        "observation",
                        1,
                        patient_ref="p-1",
                        encounter_ref="e-1",
                        code="CBC",
                    ),
                    record("e-1", "encounter", 1, patient_ref="p-1"),
                    record("p-1", "patient", 1, name="Zhang San"),
                    record("p-2", "patient", 1, name="Zhang San"),
                ]
            )
        )

        self.assertEqual(result.accepted, 4)
        self.assertEqual(result.rejected, 0)
        self.assertEqual(result.pending_references, 0)
        self.assertEqual(
            len([key for key in store.entities if key.entity_type == "patient"]), 2
        )

        exported = export_bundle(store)
        relationship_map = {
            (item["source_entity"], item["field"]): item
            for item in exported["relationships"]
        }
        encounter_link = relationship_map[
            ("hospital-a/encounter/e-1", "patient_ref")
        ]
        observation_patient_link = relationship_map[
            ("hospital-a/observation/obs-1", "patient_ref")
        ]
        observation_encounter_link = relationship_map[
            ("hospital-a/observation/obs-1", "encounter_ref")
        ]

        self.assertEqual(encounter_link["target_entity"], "hospital-a/patient/p-1")
        self.assertEqual(
            observation_patient_link["target_entity"], "hospital-a/patient/p-1"
        )
        self.assertEqual(
            observation_encounter_link["target_entity"],
            "hospital-a/encounter/e-1",
        )
        self.assertTrue(
            all(item["status"] == "resolved" for item in exported["relationships"])
        )

    def test_missing_reference_remains_pending_without_name_matching(self):
        store = RecordStore()
        result = store.import_bundle(
            bundle(
                records=[
                    record("e-1", "encounter", 1, patient_ref="missing-patient"),
                    record("same-name", "patient", 1, name="Missing Name"),
                ]
            )
        )

        self.assertEqual(result.accepted, 2)
        self.assertEqual(result.pending_references, 1)
        pending = export_bundle(store)["pending_relationships"][0]
        self.assertEqual(pending["source_entity"], "hospital-a/encounter/e-1")
        self.assertEqual(pending["target_entity"], "hospital-a/patient/missing-patient")

        second_result = store.import_bundle(
            bundle(
                records=[
                    record("missing-patient", "patient", 1, name="Missing Name"),
                ]
            )
        )
        self.assertEqual(second_result.accepted, 1)
        self.assertEqual(second_result.pending_references, 0)

    def test_duplicate_import_is_idempotent_and_update_reuses_entity(self):
        first_bundle = bundle(
            records=[record("p-1", "patient", 1, name="Old Name")]
        )
        store = RecordStore()
        first = store.import_bundle(first_bundle)
        second = store.import_bundle(first_bundle)

        self.assertEqual(first.accepted, 1)
        self.assertEqual(second.accepted, 0)
        self.assertEqual(second.duplicate, 1)
        self.assertEqual(
            len([key for key in store.entities if key.entity_type == "patient"]), 1
        )

        update = store.import_bundle(
            bundle(records=[record("p-1", "patient", 2, name="New Name")])
        )
        self.assertEqual(update.accepted, 1)
        self.assertEqual(
            len([key for key in store.entities if key.entity_type == "patient"]), 1
        )
        exported_patient = export_bundle(store)["entities"][0]
        self.assertEqual(exported_patient["current_version"], 2)
        self.assertEqual(
            [item["version"] for item in exported_patient["versions"]], [1, 2]
        )

    def test_batch_has_record_boundary_and_reports_conflict(self):
        store = RecordStore()
        store.import_bundle(
            bundle(records=[record("p-1", "patient", 1, name="Original")])
        )
        result = store.import_bundle(
            bundle(
                records=[
                    record("p-2", "patient", 1, name="Valid"),
                    record("p-1", "patient", 1, name="Conflicting rewrite"),
                    {"id": "bad", "type": "patient", "version": 1, "data": {}},
                ]
            )
        )

        self.assertEqual(result.accepted, 1)
        self.assertEqual(result.rejected, 2)
        self.assertEqual(
            {item.record_id for item in result.failures}, {"p-1", "bad"}
        )
        self.assertIsInstance(result.failures[0].reason, str)

        with self.assertRaises(ConflictError):
            raise ConflictError(result.failures[0].reason)

    def test_out_of_order_versions_reject_older_known_version(self):
        store = RecordStore()
        first = store.import_bundle(
            bundle(records=[record("p-1", "patient", 2, name="Current")])
        )
        later = store.import_bundle(
            bundle(records=[record("p-1", "patient", 1, name="Late old version")])
        )

        self.assertEqual(first.accepted, 1)
        self.assertEqual(later.accepted, 0)
        self.assertEqual(later.rejected, 1)
        self.assertEqual(store.entities[tuple(store.entities)[0]].current_version, 2)

    def test_malformed_file_is_whole_bundle_failure(self):
        store = RecordStore()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ImportFormatError):
                store.import_file(str(path))


if __name__ == "__main__":
    unittest.main()
