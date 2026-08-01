Update the existing living report for this external-review iteration.

Read the newest gate JSON and include its path and head as evidence. Only when
the snapshot is blocked, mark `external-review` as `active`, name its concise
blocker summary, and set the next action to that specific blocker (never a
generic "waiting"). When the snapshot is passing for `delivery.head_sha`, mark
the stage `passed`, name no blocker, and set the next action to protected
merge. The terminal gate performs a final current-head confirmation and writes
the authoritative passing report after the loop check.

Run `report_publish_command` with `DELIVERY_REPORT_DIR` when configured. Close
with `gc.outcome=pass`. Do not invoke provider-native subagents.
