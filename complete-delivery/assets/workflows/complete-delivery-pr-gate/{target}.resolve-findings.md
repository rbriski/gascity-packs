Resolve valid findings from the current-head gate snapshot.

Read the gate JSON, failed-check logs, every unresolved review thread in full,
and current diff. Reproduce each concern. Apply the smallest correct fix and
focused regression coverage for valid findings. For invalid or already
superseded findings, respond with concrete evidence.

Keep every thread open while editing and committing. Write the durable handoff
artifact `<artifact_root>/delivery/external-review-handoff.json` before closing
this lane. It must name the inspected head, each thread ID, its disposition,
and the exact fix commit (or no commit for a non-actionable finding).

This lane must never push or resolve a thread. `rerun-local-gates` tests the
recorded exact commit and updates the same artifact; `publish-fixes` alone
reads it, proves the pushed PR head contains every mapped fix commit, and only
then resolves the corresponding valid threads. If publication or head
confirmation fails, leave every mapped thread open.

Do not change code merely because a bot suggested it, do not hide findings,
and do not edit gate configuration to make the current PR pass. If nothing is
actionable and only checks are pending, make no source change. Record files,
tests, and thread dispositions; close with `gc.outcome=pass`.

Do not invoke provider-native subagents.
