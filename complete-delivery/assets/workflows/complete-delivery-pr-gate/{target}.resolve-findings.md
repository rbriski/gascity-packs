Resolve valid findings from the current-head gate snapshot.

Read the gate JSON, failed-check logs, every unresolved review thread in full,
and current diff. Reproduce each concern. Apply the smallest correct fix and
focused regression coverage for valid findings. For invalid or already
superseded findings, respond with concrete evidence. Resolve a thread only
after the fix is committed and will be pushed in this iteration.

Do not change code merely because a bot suggested it, do not hide findings,
and do not edit gate configuration to make the current PR pass. If nothing is
actionable and only checks are pending, make no source change. Record files,
tests, and thread dispositions; close with `gc.outcome=pass`.

Do not invoke provider-native subagents.
