Run the imported gstack release-readiness fanout after the exact local quality
gate and its living-report milestone have passed.

Require documentation, ship, deployment, rollback, and residual-risk evidence.
For `deploy_mode=command` or `ci`, deployment readiness must name the
repository-owned command or CI path, verification command, smoke command unless
`allow_no_smoke=true`, production target, and exact deployed-SHA proof. For
`deploy_mode=not-applicable`, require an evidence-backed
`deploy_not_applicable_reason` and retain the required merge-SHA proof. A
missing required contract for the selected mode is an iterate verdict, not a
non-blocking note.

Do not invoke provider-native subagents. Continue only through this Formula v2
expansion.
