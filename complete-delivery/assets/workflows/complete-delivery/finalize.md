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
review, merge, deploy, and verification still follow. Record the absolute path
on the workflow root as `gc.build.final_report_path`.

On repair attempts, read validator errors from `gc.attempt_log` and repair the
artifact in place. Close with `gc.outcome=pass`; the graph's shared artifact
validator is authoritative. Do not publish or invoke provider-native
subagents from this stage.
