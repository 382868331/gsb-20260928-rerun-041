from __future__ import annotations

import unittest

from medical_exchange import RecordStore, export_bundle
from medical_exchange.exceptions import ConflictError


def record(record_id, record_type, version, **data):
    return {"id": record_id, "type": record_type, "version": version, "data": data}


def bundle(source_id="hospital-a", records=(), name="Hospital A"):
    return {
        "format": "medlocal.v1",
        "source": {"id": source_id, "name": name},
        "records": list(records),
    }


def observation(code, version, unit="g/L"):
    return record(
        "o-1",
        "observation",
        version,
        patient_ref="p-1",
        encounter_ref="e-1",
        code=code,
        unit=unit,
    )


def store_with_conflict():
    store = RecordStore()
    store.import_bundle(
        bundle(
            records=[
                record("p-1", "patient", 1, name="Zhang San"),
                record("e-1", "encounter", 1, patient_ref="p-1"),
                observation("CBC", 1),
            ]
        )
    )
    exported = export_bundle(store, label="research-1")
    notice, _ = store.retract_version(
        "hospital-a", "observation", "o-1", 1, reason="wrong code"
    )
    store.import_bundle(bundle(records=[observation("CBC-NEW", 2)]))
    store.import_bundle(bundle(records=[observation("CBC-ALT", 3, unit="g/dL")]))
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
        retained=[
            {
                "entity": "hospital-a/observation/o-1",
                "version": 1,
                "restriction": "unit for reference only",
            }
        ],
        operator="curator-1",
        reason="first correction",
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
                "fields": ["code", "unit"],
            }
        ],
        operator="curator-2",
        reason="later correction is authoritative",
        disposition_id="disp-2",
    )
    return store, exported, notice, first, second


class FieldCandidateViewTests(unittest.TestCase):
    def test_side_by_side_original_candidates_sources_and_restrictions(self):
        store, exported, _, first, second = store_with_conflict()

        view = store.field_candidates(exported["export_id"])
        entry = view["entities"]["hospital-a/observation/o-1"]
        self.assertTrue(entry["retracted"])
        self.assertEqual(entry["original"]["code"], "CBC")
        self.assertEqual(entry["original"]["unit"], "g/L")

        code_candidates = {
            item["disposition_id"]: item for item in entry["candidates"]["code"]
        }
        self.assertEqual(code_candidates[first.id]["value"], "CBC-NEW")
        self.assertEqual(code_candidates[first.id]["operator"], "curator-1")
        self.assertEqual(code_candidates[second.id]["value"], "CBC-ALT")
        self.assertEqual(
            code_candidates[second.id]["reason"], "later correction is authoritative"
        )
        self.assertNotIn("unit", {})  # sanity
        self.assertIn("unit", entry["candidates"])

        restrictions = entry["restrictions"]
        self.assertEqual(len(restrictions), 1)
        self.assertEqual(restrictions[0]["restriction"], "unit for reference only")
        self.assertEqual(restrictions[0]["operator"], "curator-1")

    def test_export_basis_remains_queryable(self):
        store, exported, _, first, second = store_with_conflict()
        basis = store.export_basis(exported["export_id"])
        self.assertEqual(basis["label"], "research-1")
        entry = basis["versions"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["version"], 1)
        self.assertTrue(entry["retracted"])
        self.assertEqual(
            sorted(basis["dispositions"]), sorted([first.id, second.id])
        )


class FieldLevelResolutionTests(unittest.TestCase):
    def test_conflicting_fields_are_never_resolved_by_import_order(self):
        for adopt_order in (["disp-1", "disp-2"], ["disp-2", "disp-1"]):
            store, exported, _, _, _ = store_with_conflict()
            result = store.research_result(exported["export_id"], adopt=adopt_order)
            entry = result["entities"]["hospital-a/observation/o-1"]
            self.assertEqual(entry["status"], "conflict")
            self.assertEqual(entry["data"]["code"], "CBC")
            self.assertEqual(entry["data"]["unit"], "g/L")
            self.assertEqual(entry["fields"]["code"]["status"], "conflict")
            self.assertEqual(entry["fields"]["unit"]["status"], "conflict")
            self.assertEqual(len(result["conflicts"]), 2)
            conflict = next(
                item for item in result["conflicts"] if item["field"] == "code"
            )
            self.assertEqual(conflict["entity"], "hospital-a/observation/o-1")
            self.assertEqual(
                {item["disposition_id"] for item in conflict["candidates"]},
                {"disp-1", "disp-2"},
            )
            unit = next(item for item in result["conflicts"] if item["field"] == "unit")
            choices = {item["choice"] for item in unit["candidates"]}
            self.assertEqual(choices, {"replace", "retain"})

    def test_explicit_field_choice_selects_adoption_scope(self):
        store, exported, _, first, _ = store_with_conflict()
        choices = {"hospital-a/observation/o-1": {"code": first.id, "unit": first.id}}
        result = store.research_result(exported["export_id"], field_choices=choices)
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["status"], "replaced")
        self.assertEqual(entry["data"]["code"], "CBC-NEW")
        self.assertEqual(entry["data"]["unit"], "g/L")
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["fields"]["code"]["to_version"], 2)
        self.assertEqual(entry["fields"]["unit"]["status"], "restricted")
        self.assertEqual(
            entry["fields"]["unit"]["restriction"], "unit for reference only"
        )
        self.assertEqual(result["conflicts"], [])

    def test_unknown_field_choice_is_rejected(self):
        store, exported, _, _, _ = store_with_conflict()
        with self.assertRaises(ValueError):
            store.research_result(
                exported["export_id"],
                field_choices={"hospital-a/observation/o-1": {"code": "nope"}},
            )
        with self.assertRaises(ValueError):
            store.research_result(
                exported["export_id"],
                field_choices={"hospital-a/observation/unknown": {"code": "disp-1"}},
            )

    def test_partial_correction_leaves_other_fields_pending(self):
        store = RecordStore()
        store.import_bundle(
            bundle(
                records=[
                    record("p-1", "patient", 1, name="Zhang San"),
                    record("e-1", "encounter", 1, patient_ref="p-1"),
                    observation("CBC", 1),
                ]
            )
        )
        exported = export_bundle(store, label="research-partial")
        notice, _ = store.retract_version(
            "hospital-a", "observation", "o-1", 1, reason="wrong code"
        )
        store.import_bundle(bundle(records=[observation("CBC-NEW", 2, unit="mmol/L")]))
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
            operator="curator-1",
        )

        result = store.research_result(exported["export_id"])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["fields"]["unit"]["status"], "pending")
        self.assertEqual(entry["fields"]["unit"]["value"], "g/L")
        self.assertEqual(
            result["pending_fields"]["hospital-a/observation/o-1"],
            ["encounter_ref", "patient_ref", "unit"],
        )


class PendingStateMigrationTests(unittest.TestCase):
    def test_pending_state_survives_move_to_another_store(self):
        store, exported, notice, first, _ = store_with_conflict()
        export_id = exported["export_id"]

        partial = store.research_result(export_id, adopt=[first.id])
        entry = partial["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["fields"]["unit"]["status"], "restricted")

        migrated = export_bundle(store)
        restored = RecordStore()
        first_import = restored.import_bundle(migrated)
        self.assertEqual(first_import.dispositions_imported, 2)
        second_import = restored.import_bundle(migrated)
        self.assertEqual(second_import.dispositions_duplicate, 2)
        self.assertEqual(second_import.dispositions_imported, 0)
        self.assertEqual(len(restored.dispositions), 2)

        result = restored.research_result(export_id, adopt=[first.id])
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["fields"]["unit"]["status"], "restricted")

        both = restored.research_result(export_id)
        entry = both["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["status"], "conflict")
        self.assertEqual(entry["data"]["code"], "CBC")
        self.assertEqual(len(both["conflicts"]), 2)

        basis = restored.export_basis(export_id)
        self.assertTrue(basis["versions"]["hospital-a/observation/o-1"]["retracted"])

    def test_second_decision_arriving_after_migration_keeps_conflict(self):
        store, exported, notice, first, _ = store_with_conflict()
        export_id = exported["export_id"]

        migrated = export_bundle(store)
        migrated["dispositions"] = [
            item for item in migrated["dispositions"] if item["id"] == first.id
        ]
        restored = RecordStore()
        restored.import_bundle(migrated)
        self.assertEqual(len(restored.dispositions), 1)

        second, created = restored.propose_disposition(
            "export",
            export_id,
            notice.id,
            replacements=[
                {
                    "entity": "hospital-a/observation/o-1",
                    "from_version": 1,
                    "to_version": 3,
                    "fields": ["code", "unit"],
                }
            ],
            operator="curator-2",
            disposition_id="disp-2",
        )
        self.assertTrue(created)

        result = restored.research_result(export_id)
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["status"], "conflict")
        self.assertEqual(entry["data"]["code"], "CBC")

        resolved = restored.research_result(
            export_id,
            field_choices={
                "hospital-a/observation/o-1": {"code": second.id, "unit": second.id}
            },
        )
        entry = resolved["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["data"]["code"], "CBC-ALT")
        self.assertEqual(entry["data"]["unit"], "g/dL")
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["fields"]["unit"]["status"], "replaced")


class RecordedFieldDecisionTests(unittest.TestCase):
    def test_recorded_choice_survives_save_and_exchange(self):
        store, exported, _, first, _ = store_with_conflict()
        export_id = exported["export_id"]

        decision, created = store.adopt_fields(
            export_id,
            {"hospital-a/observation/o-1": {"code": first.id}},
            operator="curator-1",
            reason="partial adoption of first correction",
            decision_id="fd-1",
        )
        self.assertTrue(created)
        self.assertEqual(decision.operator, "curator-1")
        self.assertEqual(
            decision.choices, {"hospital-a/observation/o-1": {"code": first.id}}
        )

        result = store.research_result(export_id)
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["data"]["code"], "CBC-NEW")
        self.assertEqual(entry["fields"]["unit"]["status"], "conflict")
        self.assertEqual(result["field_decisions"], ["fd-1"])

        migrated = export_bundle(store)
        self.assertEqual(len(migrated["field_decisions"]), 1)
        restored = RecordStore()
        first_import = restored.import_bundle(migrated)
        self.assertEqual(first_import.field_decisions_imported, 1)
        second_import = restored.import_bundle(migrated)
        self.assertEqual(second_import.field_decisions_imported, 0)
        self.assertEqual(second_import.field_decisions_duplicate, 1)
        self.assertEqual(len(restored.field_decisions), 1)

        result = restored.research_result(export_id)
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["data"]["code"], "CBC-NEW")
        self.assertEqual(entry["fields"]["unit"]["status"], "conflict")

    def test_reselection_after_second_disposition_keeps_interpretation(self):
        store, exported, notice, first, _ = store_with_conflict()
        export_id = exported["export_id"]

        store.adopt_fields(
            export_id,
            {"hospital-a/observation/o-1": {"code": first.id}},
            operator="curator-1",
            decision_id="fd-1",
        )
        partial = store.research_result(export_id)
        entry = partial["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["data"]["code"], "CBC-NEW")

        store.adopt_fields(
            export_id,
            {
                "hospital-a/observation/o-1": {
                    "code": "disp-2",
                    "unit": "disp-2",
                }
            },
            operator="curator-2",
            reason="later correction is authoritative",
            decision_id="fd-2",
        )
        result = store.research_result(export_id)
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["data"]["code"], "CBC-ALT")
        self.assertEqual(entry["data"]["unit"], "g/dL")
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")
        self.assertEqual(entry["fields"]["unit"]["status"], "replaced")
        self.assertEqual(result["conflicts"], [])

        restored = RecordStore()
        restored.import_bundle(export_bundle(store))
        result = restored.research_result(export_id)
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["data"]["code"], "CBC-ALT")
        self.assertEqual(entry["data"]["unit"], "g/dL")
        self.assertEqual(result["field_decisions"], ["fd-1", "fd-2"])

    def test_explicit_field_choices_override_recorded_decision(self):
        store, exported, _, first, _ = store_with_conflict()
        export_id = exported["export_id"]
        store.adopt_fields(
            export_id,
            {"hospital-a/observation/o-1": {"code": first.id}},
            decision_id="fd-1",
        )
        result = store.research_result(
            export_id,
            field_choices={"hospital-a/observation/o-1": {"code": "disp-2"}},
        )
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["data"]["code"], "CBC-ALT")

    def test_invalid_current_field_decision_waits_for_new_choice(self):
        store = RecordStore()
        store.import_bundle(
            bundle(
                records=[
                    record(
                        "p-1",
                        "patient",
                        1,
                        name="Original Name",
                        gender="original",
                    ),
                    record("e-1", "encounter", 1, patient_ref="p-1"),
                ]
            )
        )
        exported = export_bundle(store, label="name-reselection")
        export_id = exported["export_id"]
        notice, _ = store.retract_version(
            "hospital-a", "patient", "p-1", 1, reason="name source withdrawn"
        )
        store.import_bundle(
            bundle(
                records=[
                    record("p-1", "patient", 2, name="dA", gender="a"),
                ]
            )
        )
        store.import_bundle(
            bundle(
                records=[
                    record("p-1", "patient", 3, name="dB", gender="b"),
                ]
            )
        )
        store.import_bundle(
            bundle(
                records=[
                    record("p-1", "patient", 4, name="dC", gender="c"),
                ]
            )
        )
        for disposition_id, to_version, operator in (
            ("disp-a", 2, "curator-a"),
            ("disp-b", 3, "curator-b"),
            ("disp-c", 4, "curator-c"),
        ):
            store.propose_disposition(
                "export",
                export_id,
                notice.id,
                replacements=[
                    {
                        "entity": "hospital-a/patient/p-1",
                        "from_version": 1,
                        "to_version": to_version,
                        "fields": ["name"],
                    }
                ],
                operator=operator,
                disposition_id=disposition_id,
            )

        for decision_id, disposition_id, expected_name in (
            ("fd-1", "disp-a", "dA"),
            ("fd-2", "disp-b", "dB"),
            ("fd-3", "disp-c", "dC"),
        ):
            store.adopt_fields(
                export_id,
                {"hospital-a/patient/p-1": {"name": disposition_id}},
                decision_id=decision_id,
            )
            result = store.research_result(export_id)
            self.assertEqual(
                result["entities"]["hospital-a/patient/p-1"]["data"]["name"],
                expected_name,
            )

        store.retract_version(
            "hospital-a", "patient", "p-1", 3, reason="middle candidate withdrawn"
        )
        result = store.research_result(export_id)
        name_field = result["entities"]["hospital-a/patient/p-1"]["fields"][
            "name"
        ]
        self.assertEqual(name_field["status"], "replaced")
        self.assertEqual(name_field["value"], "dC")
        self.assertEqual(
            result["entities"]["hospital-a/patient/p-1"]["fields"][
                "gender"
            ]["status"],
            "pending",
        )

        migrated = export_bundle(store)
        restored = RecordStore()
        first_import = restored.import_bundle(migrated)
        second_import = restored.import_bundle(migrated)
        self.assertEqual(first_import.field_decisions_imported, 3)
        self.assertEqual(second_import.field_decisions_imported, 0)
        self.assertEqual(second_import.field_decisions_duplicate, 3)

        restored.retract_version(
            "hospital-a", "patient", "p-1", 4, reason="current candidate withdrawn"
        )
        result = restored.research_result(export_id)
        entry = result["entities"]["hospital-a/patient/p-1"]
        name_field = entry["fields"]["name"]
        self.assertEqual(name_field["status"], "conflict")
        self.assertEqual(name_field["invalid_disposition_id"], "disp-c")
        self.assertEqual(name_field["value"], "Original Name")
        self.assertEqual(entry["data"]["name"], "Original Name")
        self.assertEqual(
            {item["disposition_id"] for item in name_field["candidates"]},
            {"disp-a"},
        )
        self.assertEqual(entry["fields"]["gender"]["status"], "pending")
        self.assertEqual(entry["fields"]["gender"]["value"], "original")
        self.assertEqual(
            [item.id for item in restored.field_decisions_for(export_id)],
            ["fd-1", "fd-2", "fd-3"],
        )

        restored.adopt_fields(
            export_id,
            {"hospital-a/patient/p-1": {"name": "disp-a"}},
            decision_id="fd-4",
        )
        result = restored.research_result(export_id)
        entry = result["entities"]["hospital-a/patient/p-1"]
        self.assertEqual(entry["fields"]["name"]["status"], "replaced")
        self.assertEqual(entry["fields"]["name"]["disposition_id"], "disp-a")
        self.assertEqual(entry["data"]["name"], "dA")
        self.assertEqual(entry["fields"]["gender"]["status"], "pending")
        self.assertEqual(
            result["field_decisions"], ["fd-1", "fd-2", "fd-3", "fd-4"]
        )
    def test_adopt_fields_validates_and_is_idempotent(self):
        store, exported, _, first, _ = store_with_conflict()
        export_id = exported["export_id"]
        with self.assertRaises(ValueError):
            store.adopt_fields(
                export_id, {"hospital-a/observation/o-1": {"code": "nope"}}
            )
        with self.assertRaises(ValueError):
            store.adopt_fields(
                export_id, {"hospital-a/observation/unknown": {"code": first.id}}
            )
        with self.assertRaises(ValueError):
            store.adopt_fields(
                export_id,
                {"hospital-a/observation/o-1": {"patient_ref": "disp-2"}},
            )

        _, created = store.adopt_fields(
            export_id,
            {"hospital-a/observation/o-1": {"code": first.id}},
            decision_id="fd-1",
        )
        self.assertTrue(created)
        _, created = store.adopt_fields(
            export_id,
            {"hospital-a/observation/o-1": {"code": first.id}},
            decision_id="fd-1",
        )
        self.assertFalse(created)
        with self.assertRaises(ConflictError):
            store.adopt_fields(
                export_id,
                {"hospital-a/observation/o-1": {"code": "disp-2"}},
                decision_id="fd-1",
            )

    def test_retracting_chosen_candidate_source_invalidates_only_that_field(self):
        store = RecordStore()
        store.import_bundle(
            bundle(
                records=[
                    record("p-1", "patient", 1, name="Zhang San"),
                    record("e-1", "encounter", 1, patient_ref="p-1"),
                    observation("CBC", 1),
                ]
            )
        )
        exported = export_bundle(store, label="research-retract")
        export_id = exported["export_id"]
        notice, _ = store.retract_version(
            "hospital-a", "observation", "o-1", 1, reason="wrong code"
        )
        store.import_bundle(bundle(records=[observation("CBC-NEW", 2)]))
        store.propose_disposition(
            "export",
            export_id,
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
            disposition_id="disp-1",
        )
        store.adopt_fields(
            export_id,
            {"hospital-a/observation/o-1": {"code": "disp-1"}},
            decision_id="fd-1",
        )
        result = store.research_result(export_id)
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "replaced")

        store.retract_version(
            "hospital-a", "observation", "o-1", 2, reason="correction withdrawn"
        )
        result = store.research_result(export_id)
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "pending")
        self.assertEqual(entry["fields"]["code"]["value"], "CBC")
        self.assertEqual(entry["fields"]["unit"]["status"], "pending")
        self.assertEqual(
            result["pending_fields"]["hospital-a/observation/o-1"],
            ["code", "encounter_ref", "patient_ref", "unit"],
        )

        with self.assertRaises(ValueError):
            store.adopt_fields(
                export_id,
                {"hospital-a/observation/o-1": {"code": "disp-1"}},
                decision_id="fd-2",
            )

        basis = store.export_basis(export_id)
        self.assertEqual(
            basis["versions"]["hospital-a/observation/o-1"]["version"], 1
        )
        self.assertTrue(basis["versions"]["hospital-a/observation/o-1"]["retracted"])
        self.assertEqual(basis["dispositions"], ["disp-1"])

        restored = RecordStore()
        restored.import_bundle(export_bundle(store))
        result = restored.research_result(export_id)
        entry = result["entities"]["hospital-a/observation/o-1"]
        self.assertEqual(entry["fields"]["code"]["status"], "pending")
        self.assertEqual(entry["fields"]["code"]["value"], "CBC")


if __name__ == "__main__":
    unittest.main()
