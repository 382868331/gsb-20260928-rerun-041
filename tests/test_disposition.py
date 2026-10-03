from __future__ import annotations

import unittest

from medical_exchange import ConflictError, RecordStore, export_bundle
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


def observation_bundle(code="CBC", version=1):
    return bundle(
        records=[
            record("p-1", "patient", 1, name="Zhang San"),
            record("e-1", "encounter", 1, patient_ref="p-1"),
            record(
                "o-1",
                "observation",
                version,
                patient_ref="p-1",
                encounter_ref="e-1",
                code=code,
                unit="g/L",
            ),
        ]
    )


def store_with_retracted_observation():
    store = RecordStore()
    store.import_bundle(observation_bundle())
    exported = export_bundle(store, label="research-1")
    notice, _ = store.retract_version(
        "hospital-a", "observation", "o-1", 1, reason="wrong code"
    )
    return store, exported, notice


class PartialDispositionTests(unittest.TestCase):
    def test_partial_field_replacement_and_restricted_retention(self):
        store, exported, notice = store_with_retracted_observation()
        store.import_bundle(observation_bundle(code="CBC-NEW", version=2))

        disposition, created = store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            replacements=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "from_version": 1,
                    "to_version": 2,
                    "fields": ["code"],
                }
            ],
            retained=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "version": 1,
                    "restriction": "unit kept for reference only, not for dosing",
                }
            ],
            operator="curator-1",
            reason="only the code was corrected by the source",
        )
        self.assertTrue(created)
        self.assertEqual(disposition.notice_id, notice.id)
        self.assertEqual(
            disposition.source_versions["hospital-a/observation/o-1"], 1
        )

        result = store.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["status"], "replaced")
        self.assertEqual(entry["data"]["code"], "CBC-NEW")
        self.assertEqual(entry["data"]["unit"], "g/L")
        self.assertEqual(
            entry["restrictions"], ["unit kept for reference only, not for dosing"]
        )
        self.assertEqual(entry["dispositions"], [disposition.id])
        self.assertEqual(result["applied_dispositions"], [disposition.id])

    def test_new_version_does_not_erase_retraction_or_revalidate_history(self):
        store, exported, notice = store_with_retracted_observation()
        store.import_bundle(observation_bundle(code="CBC-NEW", version=2))

        key = EntityKey("hospital-a", "observation", "o-1")
        self.assertTrue(store.is_version_retracted(key, 1))
        self.assertFalse(store.is_version_retracted(key, 2))

        result = store.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["status"], "retracted")
        self.assertIn("hospital-a/observation/o-1", result["pending"])

        disposition, _ = store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            replacements=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "from_version": 1,
                    "to_version": 2,
                    "fields": ["code"],
                }
            ],
            operator="curator-1",
        )
        comparison = store.compare_disposition(disposition.id)
        replacement = comparison["replacements"][0]
        self.assertEqual(replacement["before"], {"code": "CBC"})
        self.assertEqual(replacement["after"], {"code": "CBC-NEW"})
        self.assertEqual(
            comparison["source_versions"]["hospital-a/observation/o-1"], 1
        )

    def test_adopt_only_selected_dispositions(self):
        store, exported, notice = store_with_retracted_observation()
        store.import_bundle(observation_bundle(code="CBC-NEW", version=2))
        first, _ = store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            replacements=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "from_version": 1,
                    "to_version": 2,
                    "fields": ["code"],
                }
            ],
            operator="curator-1",
        )
        second, _ = store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            retained=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "version": 1,
                    "restriction": "historical reference only",
                }
            ],
            operator="curator-2",
        )

        adopted = store.research_result(exported["export_id"], adopt=[first.id])
        entry = adopted["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["status"], "replaced")
        self.assertEqual(entry["restrictions"], [])

        adopted = store.research_result(exported["export_id"], adopt=[second.id])
        entry = adopted["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["status"], "restricted")
        self.assertEqual(entry["data"]["code"], "CBC")


class DispositionConflictTests(unittest.TestCase):
    def test_conflicting_proposals_are_reported_not_overwritten(self):
        store, exported, notice = store_with_retracted_observation()
        store.import_bundle(observation_bundle(code="CBC-NEW", version=2))
        store.import_bundle(observation_bundle(code="CBC-ALT", version=3))

        first, _ = store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            replacements=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "from_version": 1,
                    "to_version": 2,
                    "fields": ["code"],
                }
            ],
            operator="curator-1",
            reason="trust the first correction",
        )
        second, _ = store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            replacements=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "from_version": 1,
                    "to_version": 3,
                    "fields": ["code"],
                }
            ],
            operator="curator-2",
            reason="the later correction is authoritative",
        )

        proposals = store.dispositions_for("export", exported["export_id"])
        self.assertEqual(len(proposals), 2)
        conflicts = store.disposition_conflicts("export", exported["export_id"])
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["entity"], "hospital-a/observation/o-1")
        sides = {side["operator"]: side for side in conflict["sides"]}
        self.assertEqual(sides["curator-1"]["disposition_id"], first.id)
        self.assertEqual(sides["curator-1"]["reason"], "trust the first correction")
        self.assertEqual(sides["curator-2"]["disposition_id"], second.id)
        self.assertIn("version 3", sides["curator-2"]["detail"])

    def test_same_id_with_different_content_is_rejected(self):
        store, exported, notice = store_with_retracted_observation()
        store.import_bundle(observation_bundle(code="CBC-NEW", version=2))
        replacement = [
            {
                "entity": "hospital-a/observation/o-1",
                "from_version": 1,
                "to_version": 2,
            }
        ]
        store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            replacements=replacement,
            disposition_id="disp-1",
            operator="curator-1",
        )
        again, created = store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            replacements=replacement,
            disposition_id="disp-1",
            operator="curator-1",
        )
        self.assertFalse(created)
        self.assertEqual(again.id, "disp-1")
        with self.assertRaises(ConflictError):
            store.propose_disposition(
                "export",
                exported["export_id"],
                notice.id,
                retained=[
                    {
                        "entity": "hospital-a/observation/o-1",
                        "version": 1,
                        "restriction": "reference only",
                    }
                ],
                disposition_id="disp-1",
            )
        self.assertEqual(len(store.dispositions), 1)

    def test_disposition_on_manual_merge(self):
        store = RecordStore()
        store.import_bundle(observation_bundle())
        store.import_bundle(
            bundle(
                source_id="hospital-b",
                records=[record("x-7", "patient", 1, name="Zhang San")],
            )
        )
        merge = store.merge_patients(
            "hospital-a/patient/p-1", "hospital-b/patient/x-7", reason="verified"
        )
        notice, _ = store.retract_version("hospital-a", "patient", "p-1", 1)
        store.import_bundle(
            bundle(records=[record("p-1", "patient", 2, name="Zhang San")])
        )

        disposition, created = store.propose_disposition(
            "identity_operation",
            merge.id,
            notice.id,
            replacements=[
                {
                    "entity": "hospital-a/patient/p-1",
                    "from_version": 1,
                    "to_version": 2,
                }
            ],
            operator="clerk-9",
            reason="merge still holds for the corrected patient version",
        )
        self.assertTrue(created)
        self.assertEqual(disposition.target_kind, "identity_operation")
        self.assertEqual(disposition.source_versions["hospital-a/patient/p-1"], 1)
        self.assertEqual(disposition.source_versions["hospital-b/patient/x-7"], 1)


class DispositionMigrationTests(unittest.TestCase):
    def test_migration_carries_dispositions_and_resume_is_idempotent(self):
        store, exported, notice = store_with_retracted_observation()
        store.import_bundle(observation_bundle(code="CBC-NEW", version=2))
        store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            replacements=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "from_version": 1,
                    "to_version": 2,
                    "fields": ["code"],
                }
            ],
            retained=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "version": 1,
                    "restriction": "reference only",
                }
            ],
            operator="curator-1",
            disposition_id="disp-1",
        )
        migrated_bundle = export_bundle(store)
        self.assertEqual(len(migrated_bundle["dispositions"]), 1)

        restored = RecordStore()
        first = restored.import_bundle(migrated_bundle)
        self.assertEqual(first.dispositions_imported, 1)
        self.assertEqual(len(restored.dispositions), 1)

        second = restored.import_bundle(migrated_bundle)
        self.assertEqual(second.dispositions_duplicate, 1)
        self.assertEqual(second.dispositions_imported, 0)
        self.assertEqual(len(restored.dispositions), 1)

        result = restored.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["status"], "replaced")
        self.assertEqual(entry["data"]["code"], "CBC-NEW")
        self.assertEqual(entry["restrictions"], ["reference only"])
        self.assertEqual(entry["dispositions"], ["disp-1"])


class DispositionTraceabilityTests(unittest.TestCase):
    def test_notice_before_data_then_partial_replacement_traces_everywhere(self):
        store = RecordStore()
        store.import_bundle(
            bundle(
                records=[record("p-1", "patient", 1, name="Zhang San")],
                retractions=[
                    {
                        "record_id": "o-1",
                        "type": "observation",
                        "version": 1,
                        "reason": "wrong code",
                    }
                ],
            )
        )
        self.assertEqual(len(store.pending_retractions()), 1)

        store.import_bundle(
            bundle(
                records=[
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
        )
        self.assertEqual(store.pending_retractions(), [])
        notice = store.retractions[0]

        exported = export_bundle(store, label="research-late")
        store.import_bundle(
            bundle(
                records=[
                    record(
                        "o-1",
                        "observation",
                        2,
                        patient_ref="p-1",
                        encounter_ref="e-1",
                        code="CBC-NEW",
                    )
                ]
            )
        )
        disposition, _ = store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            replacements=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "from_version": 1,
                    "to_version": 2,
                    "fields": ["code"],
                }
            ],
            operator="curator-1",
        )

        key = EntityKey("hospital-a", "observation", "o-1")
        first_recorded = store.entities[key].versions[1].recorded_at
        historical = store.visible_entities(at=first_recorded)
        self.assertNotIn(key, historical)

        current = store.visible_entities()
        self.assertEqual(current[key].version, 2)
        self.assertEqual(current[key].data["code"], "CBC-NEW")

        result = store.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["status"], "replaced")
        self.assertEqual(entry["dispositions"], [disposition.id])

        timeline = store.entity_timeline("hospital-a/observation/o-1")
        notice_event = next(
            item for item in timeline if item["event"] == "retraction_notice"
        )
        self.assertEqual(notice_event["notice_id"], notice.id)
        self.assertEqual(notice_event["status"], "applied")


if __name__ == "__main__":
    unittest.main()
