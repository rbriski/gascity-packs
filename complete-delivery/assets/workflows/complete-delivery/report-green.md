Update the living report from the passing PR gate JSON.

Verify `<artifact_root>/delivery/pr-gate.json` says `passed: true` for the
current `delivery.head_sha`. Mark stage `external-review` as `passed`, naming
the required checks, CodeRabbit signal, zero unresolved threads, and gate
artifact as evidence. Set the next action to protected merge. Run
`report_publish_command` with `DELIVERY_REPORT_DIR` when configured.

Close with `gc.outcome=pass`. Do not invoke provider-native subagents.
