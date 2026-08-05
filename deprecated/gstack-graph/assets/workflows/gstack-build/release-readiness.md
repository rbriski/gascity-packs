Prepare for gstack release readiness.

This stage adapts upstream document-release, ship, and land-and-deploy into
Gas City lanes. It runs after the `qa` anchor approves and before the build
`finalize` step; the check-gated readiness loop is the gate for finalization,
and the approved readiness summary must be recorded on the workflow root at
`gc.build.release_readiness_summary_path` before finalize begins. The lanes
check documentation drift, ship readiness, deployment readiness, and final
release risk.

Current review_mode is {{review_mode}}. In report mode, write a release
readiness report without pushing, opening PRs, merging, or deploying.

Close with `gc.outcome=pass` after release readiness context is ready.

Do not invoke provider-native subagents. Gas City fanouts own release checks.
