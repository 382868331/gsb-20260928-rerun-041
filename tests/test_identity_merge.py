from __future__ import annotations

import unittest

from medical_exchange import (
    RecordStore,
    export_bundle,
    import_unified_bundle,
)
from medical_exchange.store import parse_canonical_id


def record(record_id, record_type, version, **data):
    return {"id": record_id, "type": record_type, "version": version, "data": data}


def bundle(source_id, name, records):
    return {
        "format": "medlocal.v1",
        "source": {"id": source_id, "name": name},
        "records": list(records),
    }


def build_two_source_store():
    store = RecordStore()
    store.import_bundle(
        bundle(
            "hospital-a",
            "Hospital A",
            [
                record("p-1", "patient", 1, name="Zhang Wei"),
                record("e-1", "encounter", 1, patient_ref="p-1"),
                record(
                    "o-1", "observation", 1,
                    patient_ref="p-1", encounter_ref="e-1", code="CBC",
                ),
            ],
        )
    )
    store.import_bundle(
        bundle(
            "clinic-b",
            "Clinic B",
            [
                record("cp-9", "patient", 1, name="Zhang Wei"),
                record("ce-9", "encounter", 1, patient_ref="cp-9"),
                record(
                    "co-9", "observation", 1,
                    patient_ref="cp-9", encounter_ref="ce-9", code="XRAY",
                ),
            ],
        )
    )
    return store


class PatientMergeTests(unittest.TestCase):
    def test_same_name_different_people_stay_separate_until_manual_merge(self):
        store = build_two_source_store()

        view_a = store.patient_view("hospital-a/patient/p-1")
        view_b = store.patient_view("clinic-b/patient/cp-9")
        self.assertFalse(view_a["merged"])
        self.assertEqual(view_a["encounters"], ["hospital-a/encounter/e-1"])
        self.assertEqual(view_b["encounters"], ["clinic-b/encounter/ce-9"])

        decision = store.merge_patients(
            ["hospital-a/patient/p-1", "clinic-b/patient/cp-9"],
            reason="Same national id verified at registration",
            operator="dr-li",
            decided_at="2026-09-30T09:00:00+08:00",
        )

        self.assertEqual(decision.group_id, "patient-group-1")
        self.assertEqual(
            store.active_patient_groups(),
            {
                "patient-group-1": [
                    "clinic-b/patient/cp-9",
                    "hospital-a/patient/p-1",
                ]
            },
        )

        view = store.patient_view("hospital-a/patient/p-1")
        self.assertTrue(view["merged"])
        self.assertEqual(view["patient"], "patient-group-1")
        self.assertEqual(
            view["members"],
            ["clinic-b/patient/cp-9", "hospital-a/patient/p-1"],
        )
        self.assertEqual(
            view["encounters"],
            ["clinic-b/encounter/ce-9", "hospital-a/encounter/e-1"],
        )
        self.assertEqual(
            view["observations"],
            ["clinic-b/observation/co-9", "hospital-a/observation/o-1"],
        )

        view_via_member = store.patient_view("clinic-b/patient/cp-9")
        self.assertEqual(view_via_member, view)

        exported = export_bundle(store)
        group = exported["patient_groups"][0]
        self.assertEqual(group["members"], view["members"])
        self.assertEqual(group["basis"]["reason"], decision.reason)
        self.assertEqual(
            {item["patient"] for item in group["basis"]["evidence"]},
            {"hospital-a/patient/p-1", "clinic-b/patient/cp-9"},
        )
        self.assertEqual(
            [item["action"] for item in exported["identity_decisions"]],
            ["merge"],
        )

    def test_observation_arriving_after_merge_joins_unified_patient(self):
        store = build_two_source_store()
        store.merge_patients(
            ["hospital-a/patient/p-1", "clinic-b/patient/cp-9"],
            reason="operator confirmation",
            operator="dr-li",
            decided_at="2026-09-30T09:00:00+08:00",
        )

        result = store.import_bundle(
            bundle(
                "clinic-b",
                "Clinic B",
                [
                    record("ce-10", "encounter", 1, patient_ref="cp-9"),
                    record(
                        "co-10", "observation", 1,
                        patient_ref="cp-9", encounter_ref="ce-10", code="MRI",
                    ),
                ],
            )
        )
        self.assertEqual(result.accepted, 2)
        self.assertEqual(result.identity_conflicts, [])

        view = store.patient_view("hospital-a/patient/p-1")
        self.assertIn("clinic-b/encounter/ce-10", view["encounters"])
        self.assertIn("clinic-b/observation/co-10", view["observations"])

    def test_newer_source_version_conflicts_with_merge_but_keeps_manual_link(self):
        store = build_two_source_store()
        decision = store.merge_patients(
            ["hospital-a/patient/p-1", "clinic-b/patient/cp-9"],
            reason="same verified identity",
            operator="dr-li",
            decided_at="2026-09-30T09:00:00+08:00",
        )

        result = store.import_bundle(
            bundle(
                "clinic-b",
                "Clinic B",
                [record("cp-9", "patient", 2, name="Zhang Wei Jr.")],
            )
        )
        self.assertEqual(result.accepted, 1)
        self.assertEqual(len(result.identity_conflicts), 1)
        conflict = result.identity_conflicts[0]
        self.assertEqual(conflict["record_id"], "clinic-b/patient/cp-9")
        self.assertEqual(conflict["group_id"], "patient-group-1")
        self.assertEqual(conflict["decision_id"], decision.decision_id)

        self.assertEqual(
            store.active_patient_groups()["patient-group-1"],
            ["clinic-b/patient/cp-9", "hospital-a/patient/p-1"],
        )
        entity = store.entities[parse_canonical_id("clinic-b/patient/cp-9")]
        self.assertEqual(entity.current_version, 2)
        self.assertEqual(entity.current.data["name"], "Zhang Wei Jr.")

        exported = export_bundle(store)
        merge_record = exported["identity_decisions"][0]
        evidence = next(
            item
            for item in merge_record["evidence"]
            if item["patient"] == "clinic-b/patient/cp-9"
        )
        self.assertEqual(evidence["version"], 1)
        self.assertEqual(evidence["data"]["name"], "Zhang Wei")
        group_entity = next(
            item for item in exported["entities"]
            if item["id"] == "clinic-b/patient/cp-9"
        )
        self.assertEqual(
            [item["version"] for item in group_entity["versions"]], [1, 2]
        )

    def test_unmerge_restores_original_attribution(self):
        store = build_two_source_store()
        store.merge_patients(
            ["hospital-a/patient/p-1", "clinic-b/patient/cp-9"],
            reason="same identity assumed",
            operator="dr-li",
            decided_at="2026-09-30T09:00:00+08:00",
        )
        unmerge = store.unmerge_patients(
            "patient-group-1",
            reason="Different national ids discovered; merge was erroneous",
            operator="auditor-wang",
            decided_at="2026-09-30T11:00:00+08:00",
        )
        self.assertEqual(unmerge.action, "unmerge")
        self.assertEqual(unmerge.supersedes, 1)
        self.assertEqual(store.active_patient_groups(), {})

        view_a = store.patient_view("hospital-a/patient/p-1")
        view_b = store.patient_view("clinic-b/patient/cp-9")
        self.assertFalse(view_a["merged"])
        self.assertEqual(view_a["encounters"], ["hospital-a/encounter/e-1"])
        self.assertEqual(view_a["observations"], ["hospital-a/observation/o-1"])
        self.assertEqual(view_b["encounters"], ["clinic-b/encounter/ce-9"])
        self.assertEqual(view_b["observations"], ["clinic-b/observation/co-9"])

        actions = [
            (item.action, item.reason, item.operator, item.members)
            for item in store.decisions
        ]
        self.assertEqual(actions[1][0], "unmerge")
        self.assertEqual(actions[1][3], actions[0][3])

    def test_re_merge_after_unmerge_is_allowed(self):
        store = build_two_source_store()
        store.merge_patients(
            ["hospital-a/patient/p-1", "clinic-b/patient/cp-9"],
            reason="first attempt",
            operator="dr-li",
            decided_at="2026-09-30T09:00:00+08:00",
        )
        store.unmerge_patients(
            "patient-group-1",
            reason="mistake",
            operator="auditor-wang",
            decided_at="2026-09-30T10:00:00+08:00",
        )
        again = store.merge_patients(
            ["hospital-a/patient/p-1", "clinic-b/patient/cp-9"],
            reason="new evidence confirms identity",
            operator="dr-li",
            decided_at="2026-09-30T12:00:00+08:00",
        )
        self.assertEqual(again.group_id, "patient-group-2")
        self.assertEqual(list(store.active_patient_groups()), ["patient-group-2"])

    def test_pending_reference_and_identity_work_survive_round_trip(self):
        store = build_two_source_store()
        store.import_bundle(
            bundle(
                "hospital-a",
                "Hospital A",
                [record("e-late", "encounter", 1, patient_ref="not-here")],
            )
        )
        store.merge_patients(
            ["hospital-a/patient/p-1", "clinic-b/patient/cp-9"],
            reason="same verified identity",
            operator="dr-li",
            decided_at="2026-09-30T09:00:00+08:00",
        )
        store.import_bundle(
            bundle(
                "clinic-b",
                "Clinic B",
                [record("cp-9", "patient", 2, name="Zhang Wei (updated)")],
            )
        )
        store.unmerge_patients(
            "patient-group-1",
            reason="erroneous merge",
            operator="auditor-wang",
            decided_at="2026-09-30T11:00:00+08:00",
        )

        exported = export_bundle(store)
        restored = import_unified_bundle(exported)
        re_exported = export_bundle(restored)
        self.assertEqual(re_exported, exported)

        self.assertEqual(restored.active_patient_groups(), {})
        self.assertEqual(len(restored.decisions), 2)
        self.assertEqual(
            [item.action for item in restored.decisions],
            ["merge", "unmerge"],
        )
        self.assertEqual(
            restored.decisions[0].evidence[0]["data"]["name"], "Zhang Wei"
        )
        self.assertEqual(
            restored.patient_view("hospital-a/patient/p-1")["observations"],
            ["hospital-a/observation/o-1"],
        )
        pending = re_exported["pending_relationships"]
        self.assertEqual(
            pending[0]["source_entity"], "hospital-a/encounter/e-late"
        )
        self.assertEqual(
            pending[0]["target_entity"], "hospital-a/patient/not-here"
        )

        later = RecordStore()
        later.import_bundle(
            bundle(
                "hospital-a",
                "Hospital A",
                [record("not-here", "patient", 1, name="Late Patient")],
            )
        )
        restored.import_bundle(
            bundle(
                "hospital-a",
                "Hospital A",
                [record("not-here", "patient", 1, name="Late Patient")],
            )
        )
        self.assertEqual(
            export_bundle(restored)["pending_relationships"], []
        )
        # Touch the independently built store so the fixture comparison stays honest.
        self.assertIn("hospital-a/patient/not-here", later.patient_view(
            "hospital-a/patient/not-here"
        )["patient"])

    def test_canonical_id_parsing(self):
        key = parse_canonical_id("hospital-a/patient/p-1")
        self.assertEqual((key.source_id, key.entity_type, key.source_record_id),
                         ("hospital-a", "patient", "p-1"))
        with self.assertRaises(ValueError):
            parse_canonical_id("not-a-canonical-id")


if __name__ == "__main__":
    unittest.main()
