from __future__ import annotations

import unittest

from medical_exchange import DispositionError, RecordStore, export_bundle
from medical_exchange.models import EntityKey


def record(record_id, record_type, version, **data):
    return {"id": record_id, "type": record_type, "version": version, "data": data}


def bundle(source_id="hospital-a", records=(), name="Hospital A"):
    return {
        "format": "medlocal.v1",
        "source": {"id": source_id, "name": name},
        "records": list(records),
    }


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


def retracted_export_store():
    store = RecordStore()
    store.import_bundle(patient_bundle())
    exported = export_bundle(store, label="research-1")
    notice, _ = store.retract_version(
        "hospital-a", "observation", "o-1", 1, reason="wrong code"
    )
    return store, exported, notice


class DispositionTests(unittest.TestCase):
    def test_partial_replacement_keeps_restrictions_and_pending_fields(self):
        store, exported, notice = retracted_export_store()
        store.import_bundle(
            bundle(
                records=[
                    record(
                        "o-1",
                        "observation",
                        2,
                        patient_ref="p-1",
                        encounter_ref="e-1",
                        code="CBC-2",
                    )
                ]
            )
        )
        key = EntityKey("hospital-a", "observation", "o-1")
        self.assertTrue(store.is_version_retracted(key, 1))
        self.assertFalse(store.is_version_retracted(key, 2))

        disposition = store.propose_disposition(
            "export",
            exported["export_id"],
            retraction_id=notice.id,
            replacements=[
                {
                    "field": "code",
                    "replacement_entity": "hospital-a/observation/o-1",
                    "replacement_version": 2,
                }
            ],
            restrictions=[
                {"field": "encounter_ref", "restriction": "limited-history"}
            ],
            reason="source reissued corrected code only",
            operator="reviewer-1",
        )
        self.assertEqual(disposition.retraction_id, notice.id)
        self.assertEqual(
            disposition.source_versions.get("hospital-a/observation/o-1"), 1
        )
        self.assertEqual(disposition.original_choices["label"], "research-1")

        comparison = store.compare_disposition(disposition.id)
        item = comparison["replacements"][0]
        self.assertEqual(item["field"], "code")
        self.assertEqual(item["before"], "CBC")
        self.assertEqual(item["after"], "CBC-2")

        view = store.disposition_view("export", exported["export_id"])
        by_field = {item["field"]: item for item in view["fields"]}
        self.assertEqual(by_field["code"]["status"], "replaced")
        self.assertEqual(by_field["code"]["value"], "CBC-2")
        self.assertEqual(by_field["code"]["decided_by"], disposition.id)
        self.assertEqual(by_field["encounter_ref"]["status"], "restricted")
        self.assertEqual(
            by_field["encounter_ref"]["restriction"], "limited-history"
        )
        self.assertEqual(by_field["patient_ref"]["status"], "pending")
        self.assertEqual(
            [item["id"] for item in view["decisions"]], [disposition.id]
        )

    def test_conflicting_dispositions_are_kept_not_overwritten(self):
        store, exported, notice = retracted_export_store()
        store.import_bundle(
            bundle(
                records=[
                    record(
                        "o-1",
                        "observation",
                        2,
                        patient_ref="p-1",
                        encounter_ref="e-1",
                        code="CBC-2",
                    ),
                    record(
                        "o-1",
                        "observation",
                        3,
                        patient_ref="p-1",
                        encounter_ref="e-1",
                        code="CBC-3",
                    ),
                ]
            )
        )
        first = store.propose_disposition(
            "export",
            exported["export_id"],
            retraction_id=notice.id,
            replacements=[
                {
                    "field": "code",
                    "replacement_entity": "hospital-a/observation/o-1",
                    "replacement_version": 2,
                }
            ],
            reason="v2 reviewed",
            operator="reviewer-1",
        )
        second = store.propose_disposition(
            "export",
            exported["export_id"],
            retraction_id=notice.id,
            replacements=[
                {
                    "field": "code",
                    "replacement_entity": "hospital-a/observation/o-1",
                    "replacement_version": 3,
                }
            ],
            reason="v3 supersedes v2",
            operator="reviewer-2",
        )
        dispositions = store.dispositions_for("export", exported["export_id"])
        self.assertEqual([item.id for item in dispositions], [first.id, second.id])

        conflicts = store.disposition_conflicts("export", exported["export_id"])
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["field"], "code")
        self.assertEqual(conflict["dispositions"], [first.id, second.id])
        self.assertEqual(conflict["operators"], ["reviewer-1", "reviewer-2"])
        self.assertEqual(conflict["reasons"], ["v2 reviewed", "v3 supersedes v2"])

        view = store.disposition_view("export", exported["export_id"])
        by_field = {item["field"]: item for item in view["fields"]}
        self.assertEqual(by_field["code"]["status"], "conflict")
        self.assertEqual(
            sorted(by_field["code"]["decided_by"]), sorted([first.id, second.id])
        )

    def test_disposition_on_merge_records_original_choices(self):
        store = RecordStore()
        store.import_bundle(patient_bundle())
        store.import_bundle(
            bundle(
                source_id="hospital-b",
                records=[record("x-7", "patient", 1, name="Zhang San")],
            )
        )
        merge = store.merge_patients(
            "hospital-a/patient/p-1",
            "hospital-b/patient/x-7",
            reason="verified",
        )
        notice, _ = store.retract_version("hospital-a", "patient", "p-1", 1)
        disposition = store.propose_disposition(
            "identity_operation",
            merge.id,
            retraction_id=notice.id,
            restrictions=[{"field": "name", "restriction": "limited-history"}],
            reason="retracted name still needed for billing audit",
            operator="clerk-9",
        )
        self.assertEqual(
            disposition.original_choices["canonical_patient"],
            "hospital-a/patient/p-1",
        )
        self.assertEqual(
            disposition.source_versions.get("hospital-a/patient/p-1"), 1
        )
        view = store.disposition_view("identity_operation", merge.id)
        by_field = {item["field"]: item for item in view["fields"]}
        self.assertEqual(by_field["name"]["status"], "restricted")

    def test_invalid_disposition_is_rejected(self):
        store, exported, notice = retracted_export_store()
        with self.assertRaises(DispositionError):
            store.propose_disposition(
                "export", exported["export_id"], retraction_id="missing"
            )
        with self.assertRaises(DispositionError):
            store.propose_disposition(
                "export", "missing-export", retraction_id=notice.id
            )
        with self.assertRaises(DispositionError):
            store.propose_disposition(
                "export", exported["export_id"], retraction_id=notice.id
            )
        with self.assertRaises(DispositionError):
            store.propose_disposition(
                "export",
                exported["export_id"],
                retraction_id=notice.id,
                replacements=[
                    {
                        "field": "code",
                        "replacement_entity": "hospital-a/observation/o-1",
                        "replacement_version": 1,
                    }
                ],
            )

    def test_migration_carries_dispositions_without_redeciding(self):
        store, exported, notice = retracted_export_store()
        store.import_bundle(
            bundle(
                records=[
                    record(
                        "o-1",
                        "observation",
                        2,
                        patient_ref="p-1",
                        encounter_ref="e-1",
                        code="CBC-2",
                    )
                ]
            )
        )
        disposition = store.propose_disposition(
            "export",
            exported["export_id"],
            retraction_id=notice.id,
            replacements=[
                {
                    "field": "code",
                    "replacement_entity": "hospital-a/observation/o-1",
                    "replacement_version": 2,
                }
            ],
            restrictions=[
                {"field": "encounter_ref", "restriction": "limited-history"}
            ],
            reason="partial replacement",
            operator="reviewer-1",
        )
        migrated_bundle = export_bundle(store)
        self.assertEqual(len(migrated_bundle["dispositions"]), 1)

        migrated = RecordStore()
        first = migrated.import_bundle(migrated_bundle)
        self.assertEqual(first.dispositions_imported, 1)
        self.assertEqual(len(migrated.dispositions), 1)
        restored = migrated.dispositions[0]
        self.assertEqual(restored.id, disposition.id)
        self.assertEqual(restored.replacements, disposition.replacements)
        self.assertEqual(restored.restrictions, disposition.restrictions)
        self.assertEqual(restored.original_choices, disposition.original_choices)

        view = migrated.disposition_view("export", exported["export_id"])
        by_field = {item["field"]: item for item in view["fields"]}
        self.assertEqual(by_field["code"]["status"], "replaced")
        self.assertEqual(by_field["encounter_ref"]["status"], "restricted")

        second = migrated.import_bundle(migrated_bundle)
        self.assertEqual(second.dispositions_duplicate, 1)
        self.assertEqual(second.dispositions_imported, 0)
        self.assertEqual(len(migrated.dispositions), 1)

    def test_reopened_views_trace_back_to_decisions(self):
        store, exported, notice = retracted_export_store()
        first_recorded = store.entities[
            EntityKey("hospital-a", "observation", "o-1")
        ].versions[1].recorded_at
        store.import_bundle(
            bundle(
                records=[
                    record(
                        "o-1",
                        "observation",
                        2,
                        patient_ref="p-1",
                        encounter_ref="e-1",
                        code="CBC-2",
                    )
                ]
            )
        )
        disposition = store.propose_disposition(
            "export",
            exported["export_id"],
            retraction_id=notice.id,
            replacements=[
                {
                    "field": "code",
                    "replacement_entity": "hospital-a/observation/o-1",
                    "replacement_version": 2,
                }
            ],
            reason="partial replacement",
            operator="reviewer-1",
        )
        key = EntityKey("hospital-a", "observation", "o-1")

        historical = store.visible_entities(at=first_recorded)
        self.assertEqual(historical[key].data["code"], "CBC")

        current = store.visible_entities()
        self.assertEqual(current[key].data["code"], "CBC-2")

        view = store.disposition_view("export", exported["export_id"])
        self.assertEqual(view["decisions"][0]["id"], disposition.id)
        self.assertEqual(view["decisions"][0]["retraction_id"], notice.id)

        timeline = store.entity_timeline("hospital-a/observation/o-1")
        events = [item["event"] for item in timeline]
        self.assertIn("retraction_notice", events)
        self.assertIn("disposition", events)
        disposition_event = next(
            item for item in timeline if item["event"] == "disposition"
        )
        self.assertEqual(disposition_event["disposition_id"], disposition.id)
        self.assertEqual(disposition_event["target_id"], exported["export_id"])


if __name__ == "__main__":
    unittest.main()
