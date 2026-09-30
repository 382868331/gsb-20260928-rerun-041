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
data, retained version hashes, resolved relationships, and pending relationships.
This makes patient → encounter → observation links explicit and allows their
correspondence to be checked after export.

## Manual Patient Identity

Patient links are never inferred from names or attributes. A human operator
explicitly merges two patient records with
`store.merge_patients([canonical_id, ...], reason=..., operator=..., decided_at=...)`.
The merge records the full operation basis (reason, operator, timestamp) and an
evidence snapshot of every member patient (version, content hash, data at merge
time). `store.patient_view(canonical_id)` then returns encounters and
observations from all sources under one identity; source-local ids and the full
version history of every source record remain untouched.

When a newer source version arrives for a patient that is part of an active
merge, the version is stored as history but the manual judgement is never
silently overwritten. The import result lists it under `identity_conflicts`
with the group and decision id, and the merge-time evidence keeps showing the
original data.

A mistaken merge is reversed with
`store.unmerge_patients(group_id, reason=..., operator=..., decided_at=...)`.
Decisions are an append-only log: the unmerge records the original member set
and `supersedes` the merge decision, after which each source patient only sees
its own records again. The same patients may be re-merged later; each merge
gets a new group id.

## Round-Trip Reimport

`unified.records.v1` exports additionally contain `patient_groups` (active
merges with their basis) and `identity_decisions` (the complete merge/unmerge
log, including evidence). `import_unified_bundle(exported)` rebuilds a fresh
store: sources, entities (current data plus retained version hashes),
resolved and pending relationships, and every identity decision are restored.
Re-importing a missing referenced record into the rebuilt store resolves the
pending reference exactly as in the original store. Current-version data is
verified against its content hash on reimport.

Run tests with:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
