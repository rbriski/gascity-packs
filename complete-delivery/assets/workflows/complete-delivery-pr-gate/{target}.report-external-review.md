Update the existing living report for this external-review iteration.

Read the newest gate JSON and include its path and head as evidence. Accept it
only when it is well-formed fresh evidence with a canonical full `head_sha`
exactly equal to workflow-root `delivery.head_sha`; otherwise invalidate
`tested_commit`, `local_gates`, `published_head`, and
`published_head_matches_tested_commit`, record the blocker, and fail closed.
Keep
`external-review` `active` in every pre-terminal iteration: this lane is
evidence reporting, not passing-report authority. When the snapshot is
blocked, name its concise blocker summary and set the next action to that
specific blocker (never a generic "waiting"). When the snapshot is passing
for `delivery.head_sha`, name no blocker and set the immediate next action to
the `external-review-loop` terminal mechanical check. Only after that check
passes may the existing post-check finalizer mark the stage `passed` and
publish the protected-merge next action after its final current-head confirmation.

Run `report_publish_command` with `DELIVERY_REPORT_DIR` when configured. A
fresh valid blocked snapshot may close with `gc.outcome=pass`; close with
`gc.outcome=pass` only after valid evidence has been recorded. If evidence is
missing, malformed, stale, or root-head-mismatched, close with a non-pass
outcome. Do not invoke provider-native subagents.
