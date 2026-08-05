Complete and publish the single living delivery report.

Read merge, deployment, production verification, PR gate, review, QA, and
local-gate evidence. Update stage `deploy` and `verify` as `passed` (or deploy
as `skipped` only for the explicit non-applicable contract), then update stage
`complete` as `passed`. Set the exact delivery SHA, PR URL, configured
production URL, and next action "No action required; monitor normal production
telemetry." Use only the materialized `.gc/scripts/delivery_report.py update`
tool from the launcher worktree.

For a real verified deployment with no nonblank `smoke_command` and
`allow_no_smoke=true`, read both workflow-root `delivery.no_smoke_reason` and
`gc.var.no_smoke_reason`. Refuse completion unless both are nonblank and exactly
equal. Pass that exact plaintext value through `--no-smoke-reason` on the final
report update so the owner can see why smoke testing was omitted. Keep the
verification artifact's SHA-256 reason label as evidence, but never substitute
the hash for the readable reason. Do not pass a stale or different reason.

The top-line status remains In Progress until the graph's
`.gc/scripts/checks/delivery-report-valid.sh` check binds the durable source,
merge/deployed identity, deploy semantics, no-smoke evidence when applicable,
and exact HTML/CSS rendering. Refuse to claim Live or close this step when any
of those inputs is missing or mismatched.

Run `report_publish_command` with `DELIVERY_REPORT_DIR` when configured. Record
the final published URL on the workflow root as `delivery.report_url` when the
command provides one. Close only after HTML, CSS, state JSON, and all links are
current. The graph validator independently checks the final milestone.

Do not invoke provider-native subagents.
