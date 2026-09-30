from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from medical_exchange import (
    ConflictError,
    IdentityError,
    ImportFormatError,
    RecordStore,
    export_bundle,
)
from medical_exchange.models import EntityKey


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

    def test_manual_merge_unifies_records_without_losing_source_records(self):
        store = RecordStore()
        store.import_bundle(bundle(source_id='hospital-a', name='Hospital A', records=[
            record('p-1', 'patient', 1, name='Zhang San'),
            record('e-a', 'encounter', 1, patient_ref='p-1'),
            record('obs-a', 'observation', 1, patient_ref='p-1', encounter_ref='e-a', code='CBC'),
        ]))
        store.import_bundle(bundle(source_id='hospital-b', name='Hospital B', records=[
            record('p-9', 'patient', 1, name='Zhang San'),
            record('e-b', 'encounter', 1, patient_ref='p-9'),
        ]))

        merge = store.merge_patients(
            'hospital-a/patient/p-1',
            'hospital-b/patient/p-9',
            reason='insurance card and wristband checked',
            operator='clerk-12',
            timestamp='2026-09-29T01:02:03Z',
            operation_id='merge-zhang',
        )
        self.assertEqual(store.canonical_patient('hospital-b/patient/p-9').canonical_id(), 'hospital-a/patient/p-1')
        unified = store.patient_records('hospital-b/patient/p-9')
        self.assertEqual([item.key.canonical_id() for item in unified['encounters']], ['hospital-a/encounter/e-a', 'hospital-b/encounter/e-b'])
        self.assertEqual([item.key.canonical_id() for item in unified['observations']], ['hospital-a/observation/obs-a'])
        self.assertEqual(merge.reason, 'insurance card and wristband checked')
        self.assertEqual(store.entities[merge.patients[1]].current.data['name'], 'Zhang San')

    def test_late_exact_reference_and_post_merge_observation_are_unified(self):
        store = RecordStore()
        first = store.import_bundle(bundle(records=[
            record('p-1', 'patient', 1, name='Zhang San'),
            record('p-2', 'patient', 1, name='Zhang San'),
            record('e-early', 'encounter', 1, patient_ref='p-late'),
        ]))
        self.assertEqual(first.pending_references, 1)
        store.import_bundle(bundle(source_id='hospital-b', records=[
            record('x-7', 'patient', 1, name='Zhang San'),
        ]))
        store.merge_patients(
            'hospital-a/patient/p-1',
            'hospital-b/patient/x-7',
            reason='manual identity confirmation',
            timestamp='2026-09-29T02:00:00Z',
            operation_id='merge-late',
        )
        late_reference = store.import_bundle(bundle(records=[record('p-late', 'patient', 1, name='Zhang San')]))
        self.assertEqual((late_reference.resolved_references, late_reference.pending_references), (1, 0))
        new_records = store.import_bundle(bundle(source_id='hospital-b', records=[
            record('e-b', 'encounter', 1, patient_ref='x-7'),
            record('obs-new', 'observation', 1, patient_ref='x-7', encounter_ref='e-b', code='XRAY'),
        ]))
        self.assertEqual(new_records.manual_decisions_preserved, 1)
        records = store.patient_records('hospital-a/patient/p-1')
        self.assertEqual([item.key.canonical_id() for item in records['observations']], ['hospital-b/observation/obs-new'])

    def test_higher_version_preserves_manual_merge_and_original_source_content(self):
        store = RecordStore()
        store.import_bundle(bundle(records=[record('p-1', 'patient', 1, name='Zhang San')]))
        store.import_bundle(bundle(source_id='hospital-b', records=[record('x-7', 'patient', 1, name='Zhang San')]))
        store.merge_patients(
            'hospital-a/patient/p-1',
            'hospital-b/patient/x-7',
            reason='human review',
            timestamp='2026-09-29T03:00:00Z',
            operation_id='merge-stable',
        )
        update = store.import_bundle(bundle(source_id='hospital-b', records=[
            record('x-7', 'patient', 2, name='Zhang San (updated demographics)'),
        ]))
        self.assertEqual(update.accepted, 1)
        self.assertEqual(update.manual_decisions_preserved, 1)
        self.assertEqual(store.canonical_patient('hospital-b/patient/x-7').canonical_id(), 'hospital-a/patient/p-1')
        updated_key = EntityKey('hospital-b', 'patient', 'x-7')
        self.assertEqual(store.entities[updated_key].current_version, 2)
        self.assertEqual(store.entities[updated_key].current.data['name'], 'Zhang San (updated demographics)')

    def test_revert_merge_restores_ownership_and_retains_evidence(self):
        store = RecordStore()
        store.import_bundle(bundle(records=[
            record('p-1', 'patient', 1, name='Zhang San'),
            record('e-a', 'encounter', 1, patient_ref='p-1'),
        ]))
        store.import_bundle(bundle(source_id='hospital-b', records=[
            record('x-7', 'patient', 1, name='Zhang San'),
            record('e-b', 'encounter', 1, patient_ref='x-7'),
        ]))
        store.merge_patients(
            'hospital-a/patient/p-1',
            'hospital-b/patient/x-7',
            reason='incorrect initial review',
            timestamp='2026-09-29T04:00:00Z',
            operation_id='merge-wrong',
        )
        store.revert_merge(
            'merge-wrong',
            reason='found distinct birth dates',
            operator='supervisor-3',
            timestamp='2026-09-29T05:00:00Z',
            operation_id='revert-wrong',
        )
        self.assertEqual(store.patient_group('hospital-a/patient/p-1'), [EntityKey('hospital-a', 'patient', 'p-1')])
        records = store.patient_records('hospital-b/patient/x-7')
        self.assertEqual([item.key.canonical_id() for item in records['encounters']], ['hospital-b/encounter/e-b'])
        operations = {item['operation']: item for item in export_bundle(store)['identity_operations']}
        self.assertIn('merge', operations)
        self.assertEqual(operations['revert_merge']['reverts'], 'merge-wrong')
        with self.assertRaises(IdentityError):
            store.revert_merge('merge-wrong')

    def test_unified_export_roundtrip_preserves_relationships_and_evidence(self):
        source_store = RecordStore()
        source_store.import_bundle(bundle(records=[
            record('p-1', 'patient', 1, name='Zhang San'),
            record('e-a', 'encounter', 1, patient_ref='p-1'),
            record('obs-a', 'observation', 1, patient_ref='p-1', encounter_ref='e-a', code='CBC'),
        ]))
        source_store.import_bundle(bundle(source_id='hospital-b', records=[record('x-7', 'patient', 1, name='Zhang San')]))
        source_store.merge_patients(
            'hospital-a/patient/p-1',
            'hospital-b/patient/x-7',
            reason='verified identity',
            timestamp='2026-09-29T06:00:00Z',
            operation_id='merge-roundtrip',
        )
        exported = export_bundle(source_store)
        restored = RecordStore()
        result = restored.import_bundle(exported)
        self.assertEqual(result.accepted, 4)
        self.assertEqual(result.manual_decisions_preserved, 1)
        self.assertEqual(restored.canonical_patient('hospital-b/patient/x-7').canonical_id(), 'hospital-a/patient/p-1')
        records = restored.patient_records('hospital-b/patient/x-7')
        self.assertEqual([item.key.canonical_id() for item in records['observations']], ['hospital-a/observation/obs-a'])
        re_exported = export_bundle(restored)
        self.assertEqual(re_exported['identity_operations'], exported['identity_operations'])
        self.assertEqual(re_exported['patient_identity_groups'], exported['patient_identity_groups'])


if __name__ == "__main__":
    unittest.main()
