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
data, every retained historical version (including its full data and content
hash), resolved relationships, pending relationships, manual identity
operations, and the current patient identity groups.
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
group evidence. Each operation also records the exact source-patient versions
to which the human decision applied; withdrawal retains the original merge
version evidence. After withdrawal, encounters and observations return to
their original source-patient ownership.

`unified.records.v1` is also importable. Importing an exported bundle into a new
`RecordStore` restores all historical versions, entities, references, pending
links, identity groups, merge operations, and withdrawal evidence. Re-importing
the same bundle is idempotent: exact versions are reported as duplicates, while
only genuinely new versions are appended. Historical snapshots may arrive out
of revision order; migration fills missing old versions without changing the
highest current version.

Run tests with:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
