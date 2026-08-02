Deploy the exact `delivery.merge_sha` through the repository-owned path.

The deploy-stage graph check owns command-mode execution and evidence. Do not
run `deploy_command` yourself and do not pre-create `deploy.log`: doing either
would make the exact-once deployment attestation ambiguous. Before closing,
record only the required CI or explicit non-applicable evidence, then follow
`deploy_mode`:

- `command`: the graph check validates the nonblank `deploy_command` and its
  strictly positive, finite `deploy_timeout` of no more than one hour, exports
  the exact `DELIVERY_SHA`, `DELIVERY_REPO`, and `DELIVERY_PR`, executes the
  command exactly once under `timeout --kill-after=5s`, and atomically records
  its hash label, timeout, outcome, child and wrapper statuses, merge SHA, and
  stdout/stderr capture paths at `<artifact_root>/delivery/deploy.log`. The
  command must consume or independently resolve `DELIVERY_SHA`.
- `ci`: locate the completed successful run for the configured
  `deploy_ci_workflow` on the configured base branch and exact merge SHA. Record
  only its numeric run ID as workflow-root `delivery.deploy_run_id`; do not
  author deployment evidence or assume push-to-main means production is
  current. The graph check queries the run, merged PR, exact-SHA GitHub
  deployment, successful deployment status, and configured
  `deploy_environment`, then atomically writes the structured evidence and
  durable run/workflow/environment metadata.
- `not-applicable`: require a concrete `deploy_not_applicable_reason`; this is
  for non-deployable artifacts only, never an escape hatch for missing config.

For `not-applicable`, record `delivery.deploy_evidence_path` and set
`delivery.deploy_status=not_applicable`. In command and CI modes, the graph
check records the evidence path and `delivery.deploy_status=deployed` only
after its mechanical gate passes.
Do not yet set `verified` or
`delivery.deployed_sha`; the next stage owns production proof. Preserve the
last known good production state on failure and include rollback guidance.

Close with `gc.outcome=pass` only after either a real command or CI deployment
trigger succeeds, or a valid explicit non-applicable record has captured the
concrete non-deployable-artifact reason and deploy evidence.
Do not invoke provider-native subagents.
