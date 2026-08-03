Update the living report from the passing PR gate JSON.

Verify `<artifact_root>/delivery/pr-gate.json` says `passed: true` for the
current `delivery.head_sha`. Immediately before running
`report_publish_command`, run
`.gc/scripts/checks/delivery-external-review-deadline.sh --validate`. On
failure, invalidate the handoff's `tested_commit`, `local_gates`,
`published_head`, and `published_head_matches_tested_commit` pass evidence,
write no passing report state, do not publish, and close with a non-pass
outcome. Run `report_publish_command` with `DELIVERY_REPORT_DIR` only after
that validation when configured, and require it to succeed. A publication
failure leaves the report in its prior non-passing state, invalidates the
handoff's `tested_commit`, `local_gates`, `published_head`, and
`published_head_matches_tested_commit` pass evidence, and closes non-pass.

Immediately after successful publication (or after confirming publication is
not configured) and immediately before the sole passing report mutation, run
`.gc/scripts/checks/delivery-external-review-deadline.sh --validate` again.
On failure, invalidate the same handoff authority, write no passing report
state, and close with a non-pass outcome. Only after this final validation may
this sole outer report authority atomically mark stage `external-review` as
`passed`, writing the state document at `delivery.report_state_path` with
`schema` set to `gc.complete-delivery.report.v1` and `sha` set to the
workflow-root `delivery.head_sha`, naming the required checks, CodeRabbit
signal, zero unresolved threads, and the resolved
`delivery.pr_gate_path` in that stage's `evidence` list, and set `next_action`
to exactly `Proceed to protected merge.` Do not attempt a compensating revert:
no passing report state exists until publication and the final deadline
validation have both succeeded.

Close with `gc.outcome=pass` only after that durable passing state is written.
Otherwise close with a non-pass outcome. Do not invoke provider-native
subagents.
