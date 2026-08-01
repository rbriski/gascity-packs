Finalize the passing external-review expansion.

Read `<artifact_root>/delivery/pr-gate.json` and require `passed: true`, zero
unresolved threads, no human change requests, successful required checks, and
the configured CodeRabbit posture on the same `delivery.head_sha`. Record the
gate path on the workflow root as `delivery.pr_gate_path`.

Close with `gc.outcome=pass`. Do not merge or invoke provider-native
subagents.
