Run and repair the exact repository-native quality gates.

From the launcher worktree, run
`.gc/scripts/checks/delivery-local-gates.sh` with this claimed bead as
`GC_BEAD_ID`. The script executes every configured non-empty command in this
order: setup, lint, typecheck, test, build, browser, security, extra. On a
failure, diagnose the defect, make the smallest correct code or test change,
and rerun the full sequence. Make at most one complete repair-and-rerun attempt
per Formula iteration. If a second repair would be required, stop
mutating the checkout, preserve blocker evidence and any applicable rollback
or recovery guidance in the gate summary, and close with a non-pass outcome.
Never weaken, skip, or rewrite a configured command to obtain green output.

On a graph repair attempt, read `gc.attempt_log` first and address the recorded
failure. Write a concise gate summary under `<artifact_root>/delivery/` and
record it on the workflow root as `delivery.local_gate_summary_path`. A passing
summary must record `status=passed` and the full final `tested_commit`; confirm
that the checkout is clean and `HEAD` still equals that commit before recording
success. Before recording or trusting `delivery.local_gate_summary_path`,
canonicalize it and require a nonempty regular, non-symlink file contained
within `<artifact_root>/delivery`. Any failed, skipped, unavailable, or
head-mismatched gate must clear
or overwrite passing evidence as failed.

The graph check reruns the commands mechanically. Close with
`gc.outcome=pass` only after a local rerun is green. Do not invoke
provider-native subagents.
