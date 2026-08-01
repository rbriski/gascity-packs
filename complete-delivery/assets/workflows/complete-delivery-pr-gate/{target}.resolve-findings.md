Resolve valid findings from the current-head gate snapshot.

Read the gate JSON, failed-check logs, every unresolved review thread in full,
and current diff. Reproduce each concern. Apply the smallest correct fix and
focused regression coverage for valid findings. For invalid or already
superseded findings, respond with concrete evidence.

Keep every thread open while editing and committing. Write the durable handoff
artifact `<artifact_root>/delivery/external-review-handoff.json` before closing
this lane. It must name a full-SHA `inspected_head`, each thread ID, its
disposition, and that thread's separate `fix_commit` (or no commit for a
non-actionable finding). It must also always name a full-SHA
`candidate_commit`: use `inspected_head` when no source fix exists, otherwise
use the final committed `HEAD` after every valid source fix in this iteration.
Never use an individual thread's `fix_commit` as `candidate_commit` when later
fixes exist.

This lane must never push or resolve a thread. `rerun-local-gates` tests the
recorded `candidate_commit` and records it as `tested_commit` in the same
artifact; `publish-fixes` alone
reads it, and resolves a corresponding valid thread only when its refreshed
`published_head` is exactly equal to the artifact's `tested_commit`. Commit
containment alone is not sufficient. If publication or exact-head confirmation
fails, leave every mapped thread open.

Do not change code merely because a bot suggested it, do not hide findings,
and do not edit gate configuration to make the current PR pass. If nothing is
actionable and only checks are pending, make no source change. Record files,
tests, and thread dispositions; close with `gc.outcome=pass`.

Do not invoke provider-native subagents.
