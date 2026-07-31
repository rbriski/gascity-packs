Complete and publish the single living delivery report.

Read merge, deployment, production verification, PR gate, review, QA, and
local-gate evidence. Update stage `deploy` and `verify` as `passed` (or deploy
as `skipped` only for the explicit non-applicable contract), then update stage
`complete` as `passed`. Set the exact delivery SHA, PR URL, configured
production URL, and next action "No action required; monitor normal production
telemetry." The top-line status must become Live.

Run `report_publish_command` with `DELIVERY_REPORT_DIR` when configured. Record
the final published URL on the workflow root as `delivery.report_url` when the
command provides one. Close only after HTML, CSS, state JSON, and all links are
current. The graph validator independently checks the final milestone.

Do not invoke provider-native subagents.
