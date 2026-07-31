Initialize the one living delivery report before substantive work proceeds.

Resolve the workflow root bead and its launcher work directory. Resolve
`gc.var.artifact_root` beneath that work directory, then use
`.gc/scripts/delivery_report.py init` to create:

- `<artifact_root>/delivery-report/state.json`
- `<artifact_root>/delivery-report/index.html`
- `<artifact_root>/delivery-report/styles.css`

Use the workflow root title as the report title when `report_title` is still
the default, and use the root description/acceptance criteria as the owner
goal. Record absolute paths on the workflow root as
`delivery.report_state_path` and `delivery.report_path`. If
`report_publish_command` is configured, export `DELIVERY_REPORT_DIR` as the
report directory and run that command after rendering.

The report must lead with outcome/status, current blocker or next action, and
verified evidence. Do not invent progress. Close with `gc.outcome=pass` only
after the report and stylesheet exist.

Do not invoke provider-native subagents.
