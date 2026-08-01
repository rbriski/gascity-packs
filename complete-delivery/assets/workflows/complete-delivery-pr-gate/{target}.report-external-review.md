Update the existing living report for this external-review iteration.

Read the newest gate JSON. Mark `external-review` as `active` with a concise
blocker summary while it is blocked, or `passed` only when the snapshot itself
is passing for `delivery.head_sha`. Include the gate path and head as evidence.
Set the next action to the specific blocker, never a generic "waiting."

Run `report_publish_command` with `DELIVERY_REPORT_DIR` when configured. Close
with `gc.outcome=pass`. Do not invoke provider-native subagents.
