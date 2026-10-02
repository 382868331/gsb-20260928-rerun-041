from __future__ import annotations

import unittest

from medical_exchange import RecordStore, export_bundle
from medical_exchange.models import EntityKey


def record(record_id, record_type, version, **data):
    return {"id": record_id, "type": record_type, "version": version, "data": data}


def bundle(source_id="hospital-a", records=(), retractions=(), name="Hospital A"):
    result = {
        "format": "medlocal.v1",
        "source": {"id": source_id, "name": name},
        "records": list(records),
    }
    if retractions:
        result["retractions"] = list(retractions)
    return result


def patient_bundle():
    return bundle(
        records=[
            record("p-1", "patient", 1, name="Zhang San"),
            record("e-1", "encounter", 1, patient_ref="p-1"),
            record(
                "o-1",
                "observation",
                1,
                patient_ref="p-1",
                encounter_ref="e-1",
                code="CBC",
            ),
        ]
    )


class RetractionTests(unittest.TestCase):
    def test_retracted_current_version_excluded_from_queries_but_history_kept(self):
        store = RecordStore()
        store.import_bundle(patient_bundle())
        notice, created = store.retract_version(
            "hospital-a", "observation", "o-1", 1, reason="wrong code"
        )
        self.assertTrue(created)
        self.assertEqual(store.notice_status(notice), "applied")

        records = store.patient_records("hospital-a/patient/p-1")
        self.assertEqual(records["observations"], [])
        self.assertEqual(len(records["encounters"]), 1)

        with_retracted = store.patient_records(
            "hospital-a/patient/p-1", include_retracted=True
        )
        self.assertEqual(len(with_retracted["observations"]), 1)

        key = EntityKey("hospital-a", "observation", "o-1")
        self.assertIn(key, store.entities)
        self.assertEqual(store.entities[key].versions[1].data["code"], "CBC")
        self.assertEqual([item.key for item in store.retracted_entities()], [key])

    def test_export_marks_retracted_versions_and_keeps_data(self):
        store = RecordStore()
        store.import_bundle(patient_bundle())
        store.retract_version("hospital-a", "observation", "o-1", 1)
        exported = export_bundle(store)

        entity = next(item for item in exported["entities"] if item["id"].endswith("o-1"))
        self.assertTrue(entity["retracted"])
        self.assertTrue(entity["versions"][0]["retracted"])
        self.assertEqual(entity["versions"][0]["data"]["code"], "CBC")
        self.assertEqual(len(exported["retractions"]), 1)
        self.assertEqual(exported["retractions"][0]["status"], "applied")

    def test_notice_before_record_stays_pending_then_applies(self):
        store = RecordStore()
        result = store.import_bundle(
            bundle(
                records=[],
                retractions=[
                    {"record_id": "o-1", "type": "observation", "version": 1}
                ],
            )
        )
        self.assertEqual(result.retractions_pending, 1)
        self.assertEqual(len(store.pending_retractions()), 1)

        store.import_bundle(patient_bundle())
        self.assertEqual(store.pending_retractions(), [])
        self.assertTrue(
            store.is_current_retracted(EntityKey("hospital-a", "observation", "o-1"))
        )

    def test_duplicate_notice_is_idempotent(self):
        store = RecordStore()
        store.import_bundle(patient_bundle())
        first, created_first = store.retract_version(
            "hospital-a", "observation", "o-1", 1, notice_id="notice-1"
        )
        second, created_second = store.retract_version(
            "hospital-a", "observation", "o-1", 1, notice_id="notice-1"
        )
        third, created_third = store.retract_version(
            "hospital-a", "observation", "o-1", 1
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertFalse(created_third)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(store.retractions), 1)

    def test_same_name_version_from_other_source_not_retracted(self):
        store = RecordStore()
        store.import_bundle(patient_bundle())
        store.import_bundle(
            bundle(
                source_id="hospital-b",
                records=[record("o-1", "observation", 1, patient_ref="p-9", encounter_ref="e-9", code="CBC")],
            )
        )
        store.retract_version("hospital-a", "observation", "o-1", 1)
        other = EntityKey("hospital-b", "observation", "o-1")
        self.assertFalse(store.is_current_retracted(other))
        self.assertTrue(
            store.is_current_retracted(EntityKey("hospital-a", "observation", "o-1"))
        )

    def test_impact_lists_merges_and_exports_depending_on_retracted_version(self):
        store = RecordStore()
        store.import_bundle(patient_bundle())
        store.import_bundle(
            bundle(source_id="hospital-b", records=[record("x-7", "patient", 1, name="Zhang San")])
        )
        merge = store.merge_patients(
            "hospital-a/patient/p-1", "hospital-b/patient/x-7", reason="verified"
        )
        exported = export_bundle(store, label="research-1")
        store.retract_version("hospital-a", "patient", "p-1", 1)

        impact = store.retraction_impact("hospital-a", "patient", "p-1", 1)
        self.assertEqual(impact["status"], "applied")
        self.assertIn(merge.id, impact["identity_operations"])
        self.assertIn(exported["export_id"], impact["exports"])
        self.assertIn("hospital-a/encounter/e-1", impact["dependent_entities"])

    def test_migration_carries_retractions_and_is_idempotent(self):
        store = RecordStore()
        store.import_bundle(patient_bundle())
        store.retract_version(
            "hospital-a", "observation", "o-1", 1, reason="wrong code"
        )
        exported = export_bundle(store)

        migrated = RecordStore()
        first = migrated.import_bundle(exported)
        self.assertEqual(first.retractions_applied, 1)
        self.assertTrue(
            migrated.is_current_retracted(
                EntityKey("hospital-a", "observation", "o-1")
            )
        )

        second = migrated.import_bundle(exported)
        self.assertEqual(second.retractions_duplicate, 1)
        self.assertEqual(len(migrated.retractions), 1)

    def test_pending_retraction_survives_migration_and_applies_later(self):
        store = RecordStore()
        store.retract_version("hospital-a", "observation", "o-1", 2)
        exported = export_bundle(store)

        migrated = RecordStore()
        result = migrated.import_bundle(exported)
        self.assertEqual(result.retractions_pending, 1)

        migrated.import_bundle(
            bundle(
                records=[
                    record("o-1", "observation", 1, patient_ref="p-1", encounter_ref="e-1", code="CBC"),
                    record("o-1", "observation", 2, patient_ref="p-1", encounter_ref="e-1", code="CBC"),
                ]
            )
        )
        self.assertEqual(migrated.pending_retractions(), [])
        key = EntityKey("hospital-a", "observation", "o-1")
        self.assertTrue(migrated.is_version_retracted(key, 2))
        self.assertFalse(migrated.is_version_retracted(key, 1))

    def test_visible_entities_and_timeline(self):
        store = RecordStore()
        store.import_bundle(patient_bundle())
        store.import_bundle(
            bundle(records=[record("o-1", "observation", 2, patient_ref="p-1", encounter_ref="e-1", code="CBC2")])
        )
        exported = export_bundle(store)

        restored = RecordStore()
        restored.import_bundle(exported)
        restored.retract_version("hospital-a", "observation", "o-1", 2)

        key = EntityKey("hospital-a", "observation", "o-1")
        current = restored.visible_entities()
        self.assertNotIn(key, current)

        first_recorded = restored.entities[key].versions[1].recorded_at
        historical = restored.visible_entities(at=first_recorded)
        self.assertEqual(historical[key].version, 1)

        timeline = restored.entity_timeline("hospital-a/observation/o-1")
        events = [(item["event"], item["version"]) for item in timeline]
        self.assertIn(("version_recorded", 1), events)
        self.assertIn(("version_recorded", 2), events)
        self.assertIn(("retraction_notice", 2), events)
        notice_event = next(item for item in timeline if item["event"] == "retraction_notice")
        self.assertEqual(notice_event["status"], "applied")


if __name__ == "__main__":
    unittest.main()
