Initialize the one living delivery report before substantive work proceeds.

Resolve the workflow root bead and its launcher work directory. Resolve
`gc.var.artifact_root` beneath that work directory, then use
`.gc/scripts/delivery_report.py init` from that work directory to create:

- `<artifact_root>/delivery-report/state.json`
- `<artifact_root>/delivery-report/index.html`
- `<artifact_root>/delivery-report/styles.css`

Before using any workflow-root title or repository state, resolve
`gc.var.source_bead_id` and read the source bead/convoy. Use the validated
`gc.var.source_title` (or the source bead title when that value is absent) as
the report title. The initial summary must lead with that requested work, not
with an already-present checkout change. Do not copy source descriptions,
acceptance criteria, or notes into the public report: source notes are never
public-report content.

Record absolute paths on the workflow root as
`delivery.report_state_path` and `delivery.report_path`. If
`report_publish_command` is configured, export `DELIVERY_REPORT_DIR` as the
report directory and run that command after rendering.

The report must lead with outcome/status, current blocker or next action, and
verified evidence. Do not invent progress. Close with `gc.outcome=pass` only
after the report and stylesheet exist.

Do not invoke provider-native subagents.
