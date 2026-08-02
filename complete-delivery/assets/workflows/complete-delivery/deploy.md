Deploy the exact `delivery.merge_sha` through the repository-owned path.

Create `<artifact_root>/delivery/deploy.log`, export `DELIVERY_SHA`,
`DELIVERY_REPO`, and `DELIVERY_PR`, then follow `deploy_mode`:

- `command`: require and run `deploy_command`; capture stdout/stderr and exit
  status. The command must consume or independently resolve `DELIVERY_SHA`.
- `ci`: prove the merge triggered the documented CI deployment and record the
  run URL/id; do not merely assume push-to-main means production is current.
- `not-applicable`: require a concrete `deploy_not_applicable_reason`; this is
  for non-deployable artifacts only, never an escape hatch for missing config.

Record `delivery.deploy_evidence_path` and set `delivery.deploy_status` to
`deployed` or `not_applicable`. Do not yet set `verified` or
`delivery.deployed_sha`; the next stage owns production proof. Preserve the
last known good production state on failure and include rollback guidance.

Close with `gc.outcome=pass` only after a real deployment trigger succeeds.
Do not invoke provider-native subagents.
