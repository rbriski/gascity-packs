Update the living report from the passing PR gate JSON.

Verify `<artifact_root>/delivery/pr-gate.json` says `passed: true` for the
current `delivery.head_sha`. Immediately before the passing report mutation,
run `.gc/scripts/checks/delivery-external-review-deadline.sh --validate`. On
failure, invalidate the handoff's `tested_commit`, `local_gates`,
`published_head`, and `published_head_matches_tested_commit` pass evidence,
write no passing report state, do not publish, and close with a non-pass
outcome. Only after that validation may this sole outer report authority mark
stage `external-review` as `passed`, naming the required checks, CodeRabbit
signal, zero unresolved threads, and gate artifact as evidence, and set the
next action to protected merge.

Immediately before running `report_publish_command`, run
`.gc/scripts/checks/delivery-external-review-deadline.sh --validate` again.
On failure, invalidate the same handoff pass evidence, make no passing report
mutation or publication, and close with a non-pass outcome. Run
`report_publish_command` with `DELIVERY_REPORT_DIR` only after that validation
when configured.

Close with `gc.outcome=pass`. Do not invoke provider-native subagents.
