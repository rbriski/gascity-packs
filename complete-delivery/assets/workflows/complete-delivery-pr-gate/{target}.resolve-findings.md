Resolve valid findings from the current-head gate snapshot.

Read the gate JSON, failed-check logs, every unresolved review thread in full,
and current diff. Reproduce each concern. Apply the smallest correct fix and
focused regression coverage for valid findings. For invalid or already
superseded findings, respond with concrete evidence.

Keep every thread open while editing, committing, and testing. Record each
thread ID with its fix commit and disposition for `publish-fixes`; that lane
must first confirm the normally pushed PR head contains the fix, then resolve
only the corresponding valid thread. If publication or head confirmation
fails, leave the thread open.

Do not change code merely because a bot suggested it, do not hide findings,
and do not edit gate configuration to make the current PR pass. If nothing is
actionable and only checks are pending, make no source change. Record files,
tests, and thread dispositions; close with `gc.outcome=pass`.

Do not invoke provider-native subagents.
