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

After that terminal check has passed, immediately re-run
`.gc/scripts/checks/delivery-external-review-deadline.sh --validate` before
the final report mutation. On failure, invalidate the handoff success evidence,
write no passing report state, and close non-pass. This post-check finalizer is
the sole authority that may then update the living report from its final
current-head gate artifact: mark `external-review` as `passed`, name the
required checks, CodeRabbit signal, zero unresolved threads, and gate path as
evidence, and set the next action to protected merge. Immediately before
running `report_publish_command`, re-run
`.gc/scripts/checks/delivery-external-review-deadline.sh --validate`; failure
invalidates the pass evidence and prevents publication. Run
`report_publish_command` with `DELIVERY_REPORT_DIR` only after that validation.

Close with `gc.outcome=pass` only after the successful terminal check and
report update; otherwise close with a non-pass outcome. Do not merge or invoke
provider-native subagents.
