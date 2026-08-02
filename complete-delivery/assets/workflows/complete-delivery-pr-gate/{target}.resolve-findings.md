Resolve valid findings from the current-head gate snapshot.

Read the gate JSON and, before reading the diff, logs, or threads, require a
clean worktree (`git status --porcelain` empty) and canonical full-SHA
`HEAD == inspected_head`. If either fails, replace the handoff with blocker-only
state and restart inspection; do not reproduce, edit, or commit. Then read
every unresolved thread in full and reproduce each concern. Apply the smallest
correct fix and focused regression coverage for valid findings. For invalid or
already superseded findings, respond with concrete evidence.

Keep every thread open while editing and committing. Write the durable handoff
artifact `<artifact_root>/delivery/external-review-handoff.json` before closing
this lane. It must name a full-SHA `inspected_head`, each thread ID, its
disposition, and that thread's separate `fix_commit` (or no commit for a
non-actionable finding). It must also always name a full-SHA
`candidate_commit`: use `inspected_head` when no source fix exists, otherwise
use the final committed `HEAD` after every valid source fix in this iteration.
Never use an individual thread's `fix_commit` as `candidate_commit` when later
fixes exist.

Before every resolution attempt, replace the entire handoff object rather than
clearing selected fields. A successful object contains only this attempt's
`inspected_head`, fresh `candidate_commit`, and current thread IDs,
dispositions, and `fix_commit` values. A fresh canonical head-matched blocked
snapshot is valid: first invalidate prior terminal-success evidence, retain its
`inspected_head`, and use it as `candidate_commit` when no source fix exists.
Only missing, malformed, stale, unavailable, or head-mismatched review input
must write only blocker state (invalid): no `inspected_head`, candidate, thread
mapping, disposition, `fix_commit`, `tested_commit`, `local_gates`,
`published_head`, or equality evidence may remain to authorize later work.
This lane must never push or resolve a thread. `rerun-local-gates` tests the
recorded `candidate_commit` and records it as `tested_commit` in the same
artifact; `publish-fixes` alone reads the published disposition evidence and
may resolve a corresponding valid thread only after its fix evidence passes,
or an invalid, superseded, or otherwise non-actionable thread only after its
recorded disposition evidence is published. Either resolution requires the
refreshed `published_head` to be exactly equal to the artifact's
`tested_commit` (that is, `published_head == tested_commit`) and
`published_head_matches_tested_commit` to be true. Commit
containment alone is not sufficient. If publication or exact-head confirmation
fails, leave every mapped thread open.

Do not change code merely because a bot suggested it, do not hide findings,
and do not edit gate configuration to make the current PR pass. If nothing is
actionable and only checks are pending, make no source change. Record files,
tests, and thread dispositions; close with `gc.outcome=pass`.

Do not invoke provider-native subagents.
