Publish this iteration's verified review fixes.

Read `<artifact_root>/delivery/external-review-handoff.json`; require its local
gates to have passed for its recorded exact commit before publishing. Require a
clean checkout and require `HEAD` to still exactly equal `tested_commit`; do
not commit, amend, or otherwise mutate the tree after testing. A newly
discovered source fix must return to `rerun-local-gates` for a fresh committed
candidate and complete retest. Push exactly `tested_commit` normally to the
existing PR branch, and never force-push. If no source changed, perform no
empty commit or push. In every iteration, including a no-push iteration,
refresh the PR and persist its full-SHA `published_head`, `tested_commit`, and boolean
`published_head_matches_tested_commit` in the handoff artifact. Record the
current head on the workflow root as `delivery.head_sha`; a new head
deliberately invalidates old CI and CodeRabbit evidence for the next loop
check.

For every mapped finding in the durable handoff artifact, resolve a valid
thread only when its recorded fix evidence has passed, and resolve an invalid,
superseded, or otherwise non-actionable thread only when its recorded
disposition evidence has been published. Every such resolution additionally
requires that `published_head` is exactly equal to the
artifact's `tested_commit`; only resolve when `published_head == tested_commit` and
`published_head_matches_tested_commit` is true. If the push or head refresh
fails or the refresh is unavailable, record a publication failure, keep every
mapped thread open, and do not record passing publication evidence. Before
another inspection or local-gate execution, reacquire a current PR head that
is a full SHA. Only when a successful refresh returns a different full-SHA
`published_head` may the mismatch be recorded for the next Formula iteration
to inspect and retest that exact refreshed head; it still keeps every mapped
thread open and cannot produce passing publication evidence.
Commit
containment alone is not sufficient to resolve a thread.

Close with `gc.outcome=pass`. Do not merge or invoke provider-native
subagents.
