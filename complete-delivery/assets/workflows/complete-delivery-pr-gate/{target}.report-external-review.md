Update the existing living report for this external-review iteration.

Before reading gate evidence, publishing the report, or any provider wait/poll,
run `.gc/scripts/checks/delivery-external-review-deadline.sh --validate`.
Do not report or wait when it fails.

Read the newest gate JSON and include its path and head as evidence. Accept it only when it is well-formed fresh evidence with a canonical full `head_sha` exactly equal to workflow-root `delivery.head_sha`.
The sole transition is a proven publication whose canonical full-SHA `published_head` exactly equals the updated workflow-root `delivery.head_sha`: its prior-inspected-head `pr-gate.json` is not current-head evidence, but retain it as transition evidence and continue to the terminal check; otherwise invalidate `tested_commit`, `local_gates`, `published_head`, and `published_head_matches_tested_commit`, record the blocker, and fail closed.
Keep `external-review` `active` in every pre-terminal iteration: this lane is evidence reporting, not passing-report authority. When the snapshot is blocked, name its concise blocker summary and set the next action to that specific blocker (never a generic "waiting").
When the snapshot is passing for `delivery.head_sha`, or the proven publication transition applies, name no blocker and set the immediate next action to the `external-review-loop` terminal mechanical check. Only after that check passes may the existing post-check finalizer mark the stage `passed`; keep `external-review` active and never reuse the prior gate artifact as current-head authority before publishing the protected-merge next action.

Run `report_publish_command` with `DELIVERY_REPORT_DIR` when configured. A fresh valid blocked snapshot may close with `gc.outcome=pass`; close with `gc.outcome=pass` only after valid evidence has been recorded. If evidence is missing, malformed, stale, or root-head-mismatched other than the exact proven publication transition above, close with a non-pass outcome. Do not invoke provider-native subagents.
