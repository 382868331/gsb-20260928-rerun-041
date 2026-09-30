# Medical Data Exchange

This round implements one explicitly scoped local exchange format, `medlocal.v1`,
and imports it into versioned unified records.

## Entity Identity

Records are keyed by `(source.id, record.type, record.id)`. An update to the same
source-local id therefore updates one internal object and never creates a second
patient with the same id. Patient names and other attributes are never used to
infer links.

`patient_ref` and `encounter_ref` always contain source-local record ids. If the
target has not arrived yet, the relationship is retained as pending. Later import
of the exact referenced id resolves it; a same-name record does not.

## Import Bundle

```json
{
  "format": "medlocal.v1",
  "source": {"id": "hospital-a", "name": "Hospital A"},
  "records": [
    {"id": "p-1", "type": "patient", "version": 1, "data": {"name": "Zhang San"}},
    {"id": "e-1", "type": "encounter", "version": 1, "data": {"patient_ref": "p-1"}},
    {
      "id": "o-1",
      "type": "observation",
      "version": 1,
      "data": {
        "patient_ref": "p-1",
        "encounter_ref": "e-1",
        "code": "CBC"
      }
    }
  ]
}
```

Malformed JSON or an unsupported envelope is a whole-bundle failure. Within a
valid envelope, records have individual success/failure boundaries. Identical
`(id, type, version, content)` is reported as `duplicate` and is not counted
again. Same version with different content is a conflict; older versions arriving
after a newer current version are rejected.

## Export

`export_bundle(store)` returns `unified.records.v1` with sources, current entity
data, retained version hashes, resolved relationships, pending relationships,
manual identity operations, and the current patient identity groups.
Each entity carries its **full** version history — every version's original
`data` and optional `summary`, not only the latest content hash — so exporting
and importing into a new store restores old revision contents verbatim.
This makes patient → encounter → observation links explicit and allows their
correspondence to be checked after export. Each relationship retains its exact
source-local target in `original_target_entity`; when that patient belongs to a
manual group, `canonical_target_entity` provides the group's canonical patient.

## Manual Patient Identity

Patient identity is never inferred from names. A confirmed link is an explicit
manual operation:

```python
store.merge_patients(
    "hospital-a/patient/p-1",
    "hospital-b/patient/x-7",
    reason="insurance card and wristband checked",
    operator="clerk-12",
)
```

Both source records remain independent entities with their own source-local ids,
versions, and original data. `patient_records(patient_id)` queries encounters and
observations through the current identity group, including records that arrive
after the merge. A later higher source version updates that source's own record
but never silently removes or rewrites the manual merge decision; import results
report the preserved decision count as `manual_decisions_preserved`.

A wrong merge can be withdrawn:

```python
store.revert_merge(merge_operation_id, reason="found distinct birth dates")
```

The merge and the withdrawal are both retained in `identity_operations` with
their operation ids, timestamps, reasons, operators, affected patients, and
group evidence. After withdrawal, encounters and observations return to their
original source-patient ownership.

`unified.records.v1` is also importable. Importing an exported bundle into a new
`RecordStore` restores the current entities, references, pending links, identity
groups, merge operations, and withdrawal evidence.

## Incremental Migration

Re-importing the same `unified.records.v1` bundle is idempotent: versions whose
content hashes already exist are reported as `duplicate`, and only genuinely new
versions are appended. A migration can therefore be interrupted and resumed by
replaying the same bundle, or grown over time by exporting newer snapshots —
later snapshots only add versions the target store does not yet have. Historical
versions arriving out of numeric order are retained as history and never
overwrite or downgrade the current view.

Records are always keyed by `(source.id, type, record.id)`, so two sources that
happen to use the same local id and name, or revise in different orders, remain
independent histories and can never overwrite each other.

Each merge and withdrawal records `patient_versions`: the exact source-local
record version each patient had at the time the human decision was made. After
migration and a later `revert_merge`, encounters and observations return to
their original source-patient ownership and per-source historical queries match
the source store, with every source revision still present.

Run tests with:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
