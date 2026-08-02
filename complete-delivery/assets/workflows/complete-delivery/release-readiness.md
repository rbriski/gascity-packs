Run the imported gstack release-readiness fanout after the exact local quality
gate and its living-report milestone have passed.

Require documentation, ship, deployment, rollback, and residual-risk evidence.
Deployment readiness must name the configured `deploy_mode`, repository-owned
command or CI path, verification command, smoke command, production target,
and exact-SHA proof. A missing required deployment contract is an iterate
verdict, not a non-blocking note.

Do not invoke provider-native subagents. Continue only through this Formula v2
expansion.
