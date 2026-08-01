Finalize the passing external-review expansion and its terminal report.

The `external-review-loop` Ralph check is the sole terminal admission decision:
it runs the existing evaluator after publication and requires its fully passing
current-head gate. This finalization lane must not repair, push, resolve a
thread, or run a competing terminal gate. Consume the evaluator-confirmed
`<artifact_root>/delivery/pr-gate.json` and
`<artifact_root>/delivery/external-review-handoff.json`, then record the gate
path on the workflow root as `delivery.pr_gate_path`.

After that terminal check has passed, update the living report from its final
current-head gate artifact: mark `external-review` as `passed`, name the
required checks, CodeRabbit signal, zero unresolved threads, and gate path as
evidence, and set the next action to protected merge. Run
`report_publish_command` with `DELIVERY_REPORT_DIR` when configured.

Close with `gc.outcome=pass`. Do not merge or invoke provider-native
subagents.
