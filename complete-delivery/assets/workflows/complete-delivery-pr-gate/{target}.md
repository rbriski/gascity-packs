Finalize the passing external-review expansion and its terminal report.

Read `<artifact_root>/delivery/pr-gate.json` and require `passed: true`, zero
unresolved threads, no human change requests, successful required checks, and
the configured CodeRabbit posture on the same `delivery.head_sha`. Record the
gate path on the workflow root as `delivery.pr_gate_path`.

After the external-review loop's terminal check has passed, update the living
report from this final current-head gate artifact: mark `external-review` as
`passed`, name the required checks, CodeRabbit signal, zero unresolved threads,
and gate path as evidence, and set the next action to protected merge. Run
`report_publish_command` with `DELIVERY_REPORT_DIR` when configured.

Close with `gc.outcome=pass`. Do not merge or invoke provider-native
subagents.
