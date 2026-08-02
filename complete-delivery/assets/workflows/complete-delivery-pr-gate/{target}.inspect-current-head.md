Snapshot the pull request's current-head delivery gate.

Read and canonicalize workflow-root `delivery.head_sha` as a full 40-character
SHA before invoking the gate. Remove any pre-existing `<artifact_root>/delivery/pr-gate.json` first, then run
`{{pack_root}}/assets/scripts/delivery_gate.py` with workflow-root repo/PR,
`required_checks`, and `coderabbit`, writing that path. Never consume a pre-existing artifact after a command failure. Accept authority only from fresh evaluator JSON with semantic
`gc.complete-delivery.pr-gate.v1` identity: exact `schema`, workflow-root `repo` and `pr_number`, Boolean `passed`: `true` only with `state: "passed"` and `false` only with `state: "blocked"`, canonical full `head_sha`, and typed
`required_checks`, `coderabbit`, `unresolved_threads`, `human_change_requests`, and `blockers` collections/objects. A blocked gate exit is expected while work remains only when that identity's canonical full `head_sha` exactly equals workflow-root `delivery.head_sha`; preserve that fresh blocked snapshot and close this
inspection lane with `gc.outcome=pass` so repair children can act. First
invalidate prior terminal-success evidence (`tested_commit`, `local_gates`,
`published_head`, and `published_head_matches_tested_commit`), retain that
canonical `inspected_head`, and, because this lane makes no source fix, record
it as `candidate_commit` for the next repair or local-gate lane.

If invocation fails without fresh valid JSON, semantic identity/shape, or canonical workflow-root head match, invalidate stale
`tested_commit`, `local_gates`, `published_head`, and
`published_head_matches_tested_commit`; write only blocker state (invalid): no
`inspected_head`, `candidate_commit`, thread mapping, disposition, `fix_commit`,
`tested_commit`, `local_gates`, `published_head`, or equality evidence may remain. An API/input error is not a finding. Record a concise blocker list; do not mutate code or invoke provider-native subagents.
