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

## Retraction Impact Dispositions

Each impacted result (a manual merge or a recorded research export) is handled
with an explicit disposition, not a blanket status flip:

```python
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
    restrictions=[{"field": "encounter_ref", "restriction": "limited-history"}],
    reason="source reissued corrected code only",
    operator="reviewer-1",
)
```

A disposition records exactly which retraction notice it answers, the source
versions the target result was built from, and the original choices (merge
canonical and group, or export label and versions). Replacements substitute
only the named fields (optionally within a research `scope`) from a chosen
newer source version; every other affected field keeps an explicit usage
restriction or stays `pending`. A replacement must point at an existing,
non-retracted version: a source re-providing similar content as a new version
does not erase the retraction or silently re-validate old results.

`store.compare_disposition(id)` shows each replaced field's value before and
after, so reviewers can adopt only selected parts. Several operators may file
dispositions for the same result; incompatible actions on the same field are
never overwritten by the later save. `store.disposition_conflicts(...)` lists
the conflicting field, the competing disposition ids, operators, and reasons,
and `store.disposition_view(...)` marks the field as `conflict` while keeping
every proposal.

Dispositions and their restrictions travel inside `unified.records.v1`;
incremental migration imports them once by id, so resuming an interrupted
migration never duplicates a decision. `store.entity_timeline(...)` includes
disposition events, and `visible_entities(at=...)`, the current valid view,
and `store.disposition_view(...)` each trace back to the disposition ids,
operators, and retractions behind them.

Incremental migration carries retraction notices and export records inside
`unified.records.v1`; resuming an interrupted migration does not duplicate
them. After a restart, `store.visible_entities()` returns the current valid
data, `store.visible_entities(at=timestamp)` reconstructs what was visible at
that time, and `store.entity_timeline(entity_id)` traces every version and
retraction state change along the source.

Run tests with:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
