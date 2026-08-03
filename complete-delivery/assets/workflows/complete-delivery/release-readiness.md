Run the imported gstack release-readiness fanout after the exact local quality
gate and its living-report milestone have passed.

Require documentation, ship, deployment, rollback, and residual-risk evidence.
For `deploy_mode=command` or `ci`, deployment readiness must name the
repository-owned command or CI path, verification command, production target,
and smoke command unless `allow_no_smoke=true`. That exception requires the
nonempty `gc.var.no_smoke_reason`; plan to record it on the workflow root as
`delivery.no_smoke_reason` with an argument-safe metadata update and to record
only its SHA-256 label in verification evidence. For
`deploy_mode=not-applicable`, require an evidence-backed
`deploy_not_applicable_reason`. This pre-deployment stage validates plans and
commands only: `verify-production` alone owns the post-deployment merge-SHA
attestation. A missing required contract for the selected mode is an iterate
verdict, not a non-blocking note.

Do not invoke provider-native subagents. Continue only through this Formula v2
expansion.
