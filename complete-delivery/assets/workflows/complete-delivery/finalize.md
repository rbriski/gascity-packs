Write the pre-publication Complete Delivery build record.

Synthesize requirements, plan, decomposition, canonical implementation
summary, internal review, QA, local gates, and release-readiness evidence. The
artifact is Markdown with YAML front matter and schema
`gc.build.final-report.v1`; use the same workflow, methodology, producer,
status, trace-upstream, trace-coverage, and exact `ID`/`Status` coverage-table
contract documented by the imported Gas City build finalizer. Set methodology
pack/name to `complete-delivery` / `complete-delivery` and producer stage to
`finalize`. Required sections are Summary, Outcome, Artifacts, and Remaining
Risks.

This is not the terminal delivery claim: say explicitly that PR, external
review, merge, deploy, and verification still follow. Write the report only
under `<artifact_root>/delivery/`, resolve its canonical path, reject
non-regular files and symlink escapes, and record only that validated absolute
path on the workflow root as `gc.build.final_report_path`.

Before finalizing, read `gc.var.source_bead_id` and the durable source bead or
convoy. Fetch exactly one durable record and require its returned `id` to equal
`gc.var.source_bead_id` byte-for-byte. Fail closed if the source ID is absent,
the durable bead or convoy cannot be read, the returned ID differs, its title
or nonblank acceptance criteria are absent, or the approved requirements,
plan, and decomposition cannot be linked back to those criteria.
In any of those cases, record the unresolved trace as a blocker and close with
a non-pass outcome; do not write a passing final report or set
`gc.outcome=pass`. Only after every value resolves may the visible, unfenced
`## Source trace` section record these exact lines: `Source ID: <source-id>`,
`Source title: <source-title>`, and `Acceptance criteria SHA-256:
sha256:<hash>`. In YAML front matter, set `source.id`, `source.title`,
`source.anchor`, and `source.acceptance_criteria_sha256` to the same resolved
values. Write each visible value as its exact raw text without Markdown
backticks or other decoration. The hash is the SHA-256 digest of the exact
acceptance-criteria string.
This trace proves the delivered outcome is the requested work rather than a
coincidental repository-HEAD change. Do not reproduce raw source notes,
acceptance criteria, or sensitive source text in the public living report.
The source-artifact validator mechanically reopens the approved artifacts at
`gc.build.requirements_path`, `gc.build.plan_path`, and
`gc.build.decomposition_path`; each must have approved status and the same
exact durable source identity and acceptance-criteria SHA-256 before this
stage can pass.

On repair attempts, read validator errors from `gc.attempt_log` and repair the artifact in place. Close with `gc.outcome=pass` only when the source trace is resolved, the Complete Delivery source-artifact validator (which invokes the shared artifact validator) accepts the final report, and no blockers remain; otherwise close with a non-pass outcome. Do not publish or invoke provider-native subagents from this stage.
