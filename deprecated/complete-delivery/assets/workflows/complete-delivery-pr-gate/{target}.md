Finalize the passing external-review expansion and its terminal report.

Before finalizer work, run
`.gc/scripts/checks/delivery-external-review-deadline.sh --validate`.
The terminal repository check repeats it before evaluating any provider state;
fail closed without publishing when validation fails.

The `external-review-loop` Ralph check is the sole terminal admission decision:
it runs the existing evaluator after publication and requires its fully passing
current-head gate. This finalization lane must not repair, push, resolve a
thread, or run a competing terminal gate. Consume the evaluator-confirmed
`<artifact_root>/delivery/pr-gate.json` and
`<artifact_root>/delivery/external-review-handoff.json`, then record the gate
path on the workflow root as `delivery.pr_gate_path`.

Reject missing, malformed, stale, or root-head-mismatched gate evidence. On
any non-success finalization path, invalidate the handoff's `tested_commit`,
`local_gates`, `published_head`, and `published_head_matches_tested_commit`
success evidence rather than allowing a prior attempt to authorize the report,
then close with a non-pass outcome.

Immediately before recording finalizer pass evidence, re-run
`.gc/scripts/checks/delivery-external-review-deadline.sh --validate`; an
expired deadline closes non-pass without recording that evidence. After that
terminal check and immediate revalidation have passed, record only the
validated current-head gate path and close with `gc.outcome=pass`. This nested finalizer is
evidence-only: it must never mark `external-review` as `passed`, set a
protected-merge next action, or invoke `report_publish_command`. Top-level
`complete-delivery/report-green.md`, which runs after this expansion, is the
sole authority for the passing living-report transition, protected merge, and
passing-report publication. Immediately before
the final report mutation, the top-level `report-green` actor must re-run the
immutable deadline validation. Immediately before
running `report_publish_command`, that same top-level actor must re-run the
immutable deadline validation; a failure prevents publication.
This finalizer records evidence only and never performs that mutation.

Close with `gc.outcome=pass` only after the successful terminal check and
evidence recording; otherwise close with a non-pass outcome. Do not merge or
invoke provider-native subagents.
