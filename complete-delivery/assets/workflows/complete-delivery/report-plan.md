Update the existing living report after plan review.

Read `delivery.report_state_path`, the approved requirements and plan
artifacts, and plan-review verdict. Run
`{{pack_root}}/assets/scripts/delivery_report.py update`
for stage `plan` with status `passed`, a one-sentence owner-facing summary, the
artifact paths as evidence, and the next action "Implement the approved plan."
Run `report_publish_command` with `DELIVERY_REPORT_DIR` when configured.

Update in place; never create a second report. Close with `gc.outcome=pass`.
Do not invoke provider-native subagents.
