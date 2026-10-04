from __future__ import annotations

import unittest

from medical_exchange import ConflictError, RecordStore, export_bundle


def record(record_id, record_type, version, **data):
    return {"id": record_id, "type": record_type, "version": version, "data": data}


def bundle(source_id="hospital-a", records=(), retractions=()):
    result = {
        "format": "medlocal.v1",
        "source": {"id": source_id, "name": "Hospital A"},
        "records": list(records),
    }
    if retractions:
        result["retractions"] = list(retractions)
    return result


def observation(version, code, unit="g/L"):
    return record(
        "o-1",
        "observation",
        version,
        patient_ref="p-1",
        encounter_ref="e-1",
        code=code,
        unit=unit,
    )


def store_with_candidates():
    store = RecordStore()
    store.import_bundle(
        bundle(
            records=[
                record("p-1", "patient", 1, name="Zhang San"),
                record("e-1", "encounter", 1, patient_ref="p-1"),
                observation(1, "CBC"),
            ]
        )
    )
    exported = export_bundle(store, label="research-1")
    notice, _ = store.retract_version(
        "hospital-a", "observation", "o-1", 1, reason="wrong code"
    )
    store.import_bundle(bundle(records=[observation(2, "CBC-NEW")]))
    store.import_bundle(bundle(records=[observation(3, "CBC-ALT")]))
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
        disposition_id="disp-1",
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
        disposition_id="disp-2",
    )
    return store, exported, notice, first, second


class CompareFieldTests(unittest.TestCase):
    def test_candidates_are_listed_side_by_side(self):
        store, exported, _, first, second = store_with_candidates()
        view = store.compare_field(
            exported["export_id"], "hospital-a/observation/o-1", "code"
        )
        self.assertEqual(view["original"]["value"], "CBC")
        self.assertEqual(view["original"]["version"], 1)
        self.assertTrue(view["original"]["retracted"])
        self.assertEqual(len(view["candidates"]), 2)
        by_operator = {item["operator"]: item for item in view["candidates"]}
        self.assertEqual(by_operator["curator-1"]["value"], "CBC-NEW")
        self.assertEqual(by_operator["curator-1"]["to_version"], 2)
        self.assertEqual(by_operator["curator-1"]["disposition_id"], first.id)
        self.assertEqual(
            by_operator["curator-2"]["reason"], "the later correction is authoritative"
        )
        self.assertEqual(by_operator["curator-2"]["value"], "CBC-ALT")

    def test_retention_candidate_shows_restriction(self):
        store, exported, notice, _, _ = store_with_candidates()
        store.propose_disposition(
            "export",
            exported["export_id"],
            notice.id,
            retained=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "version": 1,
                    "restriction": "reference only, not for dosing",
                }
            ],
            operator="curator-3",
            disposition_id="disp-3",
        )
        view = store.compare_field(
            exported["export_id"], "hospital-a/observation/o-1", "code"
        )
        retained = [c for c in view["candidates"] if c["choice"] == "retain"]
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            retained[0]["restrictions"], ["reference only, not for dosing"]
        )


class FieldAdoptionTests(unittest.TestCase):
    def test_partial_adoption_leaves_other_fields_pending(self):
        store, exported, _, first, _ = store_with_candidates()
        adoption, created = store.adopt_fields(
            exported["export_id"], first.id, fields=["code"], operator="lead-1"
        )
        self.assertTrue(created)

        result = store.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["data"]["code"], "CBC-NEW")
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["fields"]["unit"]["status"], "pending")
        self.assertIn("unit", entry["pending_fields"])
        self.assertEqual(result["adoptions"], [adoption.id])

    def test_conflicting_adoptions_are_not_resolved_by_order(self):
        store, exported, _, first, second = store_with_candidates()
        store.adopt_fields(exported["export_id"], first.id, fields=["code"])
        store.adopt_fields(exported["export_id"], second.id, fields=["code"])

        result = store.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["status"], "conflict")
        self.assertEqual(entry["fields"]["code"]["status"], "conflict")
        self.assertEqual(entry["data"]["code"], "CBC")
        self.assertEqual(
            entry["conflicts"],
            [{"field": "code", "dispositions": [first.id, second.id]}],
        )

        other, other_export, _, other_first, other_second = store_with_candidates()
        other.adopt_fields(other_export["export_id"], other_second.id, fields=["code"])
        other.adopt_fields(other_export["export_id"], other_first.id, fields=["code"])
        other_result = other.research_result(other_export["export_id"])
        other_entry = other_result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(other_entry["status"], "conflict")
        self.assertEqual(other_entry["data"]["code"], "CBC")

    def test_adopt_is_still_available_as_ephemeral_preview(self):
        store, exported, _, first, _ = store_with_candidates()
        result = store.research_result(exported["export_id"], adopt=[first.id])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(store.adoptions, [])

    def test_adoption_validation_and_idempotency(self):
        store, exported, _, first, _ = store_with_candidates()
        with self.assertRaises(ValueError):
            store.adopt_fields(exported["export_id"], first.id, fields=["unit"])
        with self.assertRaises(ValueError):
            store.adopt_fields(exported["export_id"], "disp-missing")

        adoption, created = store.adopt_fields(
            exported["export_id"], first.id, fields=["code"], adoption_id="adopt-1"
        )
        self.assertTrue(created)
        again, created = store.adopt_fields(
            exported["export_id"], first.id, fields=["code"], adoption_id="adopt-1"
        )
        self.assertFalse(created)
        self.assertEqual(again.id, adoption.id)
        with self.assertRaises(ConflictError):
            store.adopt_fields(
                exported["export_id"],
                "disp-2",
                fields=["code"],
                adoption_id="adopt-1",
            )
        self.assertEqual(len(store.adoptions), 1)


class AdoptionMigrationTests(unittest.TestCase):
    def test_pending_state_survives_exchange_and_reimport_is_idempotent(self):
        store, exported, _, first, _ = store_with_candidates()
        store.adopt_fields(
            exported["export_id"],
            first.id,
            fields=["code"],
            operator="lead-1",
            adoption_id="adopt-1",
        )
        migrated = export_bundle(store)
        self.assertEqual(len(migrated["adoptions"]), 1)

        restored = RecordStore()
        first_import = restored.import_bundle(migrated)
        self.assertEqual(first_import.adoptions_imported, 1)
        self.assertEqual(len(restored.adoptions), 1)

        result = restored.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["data"]["code"], "CBC-NEW")
        self.assertEqual(entry["fields"]["unit"]["status"], "pending")
        self.assertEqual(result["adoptions"], ["adopt-1"])

        second_import = restored.import_bundle(migrated)
        self.assertEqual(second_import.adoptions_duplicate, 1)
        self.assertEqual(second_import.adoptions_imported, 0)
        self.assertEqual(len(restored.adoptions), 1)

    def test_second_decision_arriving_after_partial_adoption(self):
        store, exported, _, first, second = store_with_candidates()
        store.adopt_fields(exported["export_id"], first.id, fields=["code"])
        migrated = export_bundle(store)

        restored = RecordStore()
        restored.import_bundle(migrated)
        result = restored.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")

        restored.adopt_fields(exported["export_id"], second.id, fields=["code"])
        result = restored.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "conflict")
        self.assertEqual(entry["fields"]["unit"]["status"], "pending")


class RetractionScopeTests(unittest.TestCase):
    def test_retracting_a_source_version_only_affects_dependent_fields(self):
        store = RecordStore()
        store.import_bundle(
            bundle(
                records=[
                    record("p-1", "patient", 1, name="Zhang San"),
                    record("e-1", "encounter", 1, patient_ref="p-1"),
                    observation(1, "CBC"),
                ]
            )
        )
        exported = export_bundle(store, label="research-1")
        notice, _ = store.retract_version("hospital-a", "observation", "o-1", 1)
        store.import_bundle(bundle(records=[observation(2, "CBC-NEW")]))
        store.import_bundle(bundle(records=[observation(3, "CBC-NEW", unit="g/dL")]))
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
                },
                {
                    "entity": "hospital-a/observation/o-1",
                    "from_version": 1,
                    "to_version": 3,
                    "fields": ["unit"],
                },
            ],
            operator="curator-1",
            disposition_id="disp-1",
        )
        store.adopt_fields(exported["export_id"], "disp-1")

        result = store.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["fields"]["unit"]["status"], "replaced")

        store.retract_version("hospital-a", "observation", "o-1", 2, reason="bad fix")
        result = store.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "pending")
        self.assertEqual(entry["data"]["code"], "CBC")
        self.assertEqual(entry["fields"]["unit"]["status"], "replaced")
        self.assertEqual(entry["data"]["unit"], "g/dL")
        self.assertIn("code", entry["pending_fields"])


class ExportBasisTests(unittest.TestCase):
    def test_old_export_basis_stays_queryable(self):
        store, exported, notice, first, _ = store_with_candidates()
        basis = store.export_basis(exported["export_id"])
        self.assertEqual(basis["export_id"], exported["export_id"])
        self.assertEqual(basis["label"], "research-1")
        entry = basis["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["version"], 1)
        self.assertTrue(entry["retracted"])
        self.assertEqual(entry["notices"], [notice.id])
        self.assertIn(first.id, entry["dispositions"])
        entity = store.entities[
            next(
                key
                for key in store.entities
                if key.canonical_id() == "hospital-a/observation/o-1"
            )
        ]
        self.assertEqual(entry["content_hash"], entity.versions[1].content_hash)
        with self.assertRaises(ValueError):
            store.export_basis("export-missing")


if __name__ == "__main__":
    unittest.main()
