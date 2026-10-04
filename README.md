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

## Source Retraction Notices

A source can retract one specific version of one of its records:

```python
store.retract_version("hospital-a", "observation", "o-1", 1, reason="wrong code")
```

Retractions may also arrive inside a `medlocal.v1` bundle under a `retractions`
key. The notice targets exactly `(source.id, record.type, record.id, version)`;
a same-named version from another source is never affected. The original data
and the notice itself are both retained; history is never deleted. Current
queries (`patient_records`) and exports no longer treat a retracted current
version as valid: queries skip it (pass `include_retracted=True` to see it),
and exports mark the entity and version with `"retracted": true`.

Notices arriving before the corresponding record, out of order, or duplicated
are kept as `pending` relationships (`store.pending_retractions()`) and apply
automatically once the exact version arrives; duplicate notices are idempotent.
`store.retraction_impact(...)` lists the manual merges, recorded research
exports, and dependent entities that relied on the retracted version, so users
can decide whether to re-select data or keep the result as limited history.

Incremental migration carries retraction notices and export records inside
`unified.records.v1`; resuming an interrupted migration does not duplicate
them. After a restart, `store.visible_entities()` returns the current valid
data, `store.visible_entities(at=timestamp)` reconstructs what was visible at
that time, and `store.entity_timeline(entity_id)` traces every version and
retraction state change along the source.

## Retraction Dispositions

A retraction does not force a single new status onto an affected result.
Instead, each operator proposes a disposition for a specific research export
or manual merge, scoped to the exact retraction notice:

```python
store.propose_disposition(
    "export",
    export_id,
    notice_id,
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
```

Each disposition records the retraction notice it answers, the source versions
the original decision relied on (`source_versions`), and the operator's
reason. Replacements may cover selected fields only; everything else keeps its
original value with an explicit usage restriction. A later source version with
similar content never erases the retraction or re-validates old exports:
`store.research_result(export_id)` still marks undisposed entities as
`retracted` (listed under `pending`) until a disposition covers them.

`store.compare_disposition(disposition_id)` shows the affected fields and
versions before and after replacement, so users can adopt only selected parts
via `store.research_result(export_id, adopt=[disposition_id, ...])`.

Multiple operators may propose incompatible dispositions for the same result.
Proposals are never overwritten: reusing a disposition id with different
content raises `ConflictError`, and `store.disposition_conflicts(...)`
reports each conflicting entity and field with both sides' operators, choices,
and reasons.

Dispositions and their restrictions travel inside `unified.records.v1` under
a `dispositions` key; importing is idempotent by disposition id, so resuming
an interrupted migration never duplicates a decision. After migration,
`store.visible_entities(at=...)`, the current valid view, and
`store.research_result(...)` each trace back to the recorded notice and
disposition ids.

## Field-Level Disposition Resolution

When two dispositions propose different new versions for the same research
field, `store.field_candidates(export_id)` shows the original value, every
candidate (disposition id, operator, reason, target version, value), and the
recorded restrictions side by side, so a user can choose the adoption scope
field by field:

```python
result = store.research_result(
    export_id,
    field_choices={"hospital-a/observation/o-1": {"code": "disp-1"}},
)
```

Conflicts are never resolved by import or proposal order. Adopting two
incompatible dispositions marks the contested field (and its entity) as
`conflict` and keeps the original value until an explicit `field_choices`
entry picks a side; a choice may also name a retention-only disposition to
keep the old value under its restriction. Every field in
`research_result(...)` carries its own status - `valid`, `replaced`,
`restricted`, `pending`, or `conflict` - so a partial correction never makes
the other fields of a retracted version silently valid: uncovered fields stay
`pending` (also listed under `pending_fields`), and a retraction only affects
the fields that actually depend on the retracted version.

Partial adoption is safe to move: after adopting only some fields, exporting
and importing into another `RecordStore` preserves the pending state, and a
second decision arriving later (proposed locally or carried in a unified
bundle) still conflicts instead of overwriting. Re-importing the same bundle
never re-registers a disposition. The basis of any old export stays queryable
via `store.export_basis(export_id)`, which returns the recorded versions,
their content hashes, retraction state, and the dispositions that answer them.

Run tests with:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
