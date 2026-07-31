Snapshot the pull request's current-head delivery gate.

Run `.gc/scripts/delivery_gate.py` with workflow-root repo/PR,
`required_checks`, and `coderabbit`, writing
`<artifact_root>/delivery/pr-gate.json`. A blocked exit is expected while work
remains: preserve the JSON and close this inspection lane with
`gc.outcome=pass` so repair children can act. An API/input error is not a
finding; diagnose it or fail closed.

Record the inspected head SHA and concise blocker list. Do not mutate code or
invoke provider-native subagents.
