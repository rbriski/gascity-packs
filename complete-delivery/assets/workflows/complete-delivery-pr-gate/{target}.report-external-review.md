Update the existing living report for this external-review iteration.

Read the newest gate JSON and include its path and head as evidence. Keep
`external-review` `active` in every pre-terminal iteration: this lane is
evidence reporting, not passing-report authority. When the snapshot is
blocked, name its concise blocker summary and set the next action to that
specific blocker (never a generic "waiting"). When the snapshot is passing
for `delivery.head_sha`, name no blocker and set the next action to the
post-check finalizer; never claim protected merge is next from this lane. Only
the existing post-check finalizer may mark the stage `passed` and publish the
protected-merge next action after its final current-head confirmation.

Run `report_publish_command` with `DELIVERY_REPORT_DIR` when configured. Close
with `gc.outcome=pass`. Do not invoke provider-native subagents.
