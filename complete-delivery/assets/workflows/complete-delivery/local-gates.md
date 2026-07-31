Run and repair the exact repository-native quality gates.

From the launcher worktree, run
`.gc/scripts/checks/delivery-local-gates.sh` with this claimed bead as
`GC_BEAD_ID`. The script executes every configured non-empty command in this
order: setup, lint, typecheck, test, build, browser, security, extra. On a
failure, diagnose the defect, make the smallest correct code or test change,
and rerun until the full sequence passes. Never weaken, skip, or rewrite a
configured command to obtain green output.

On a graph repair attempt, read `gc.attempt_log` first and address the recorded
failure. Write a concise gate summary under `<artifact_root>/delivery/` and
record it on the workflow root as `delivery.local_gate_summary_path`.

The graph check reruns the commands mechanically. Close with
`gc.outcome=pass` only after a local rerun is green. Do not invoke
provider-native subagents.
