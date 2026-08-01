Publish this iteration's verified review fixes.

Read `<artifact_root>/delivery/external-review-handoff.json`; require its local
gates to have passed for its recorded exact commit before publishing. Commit
only intentional changes with a focused message, push normally to the existing
PR branch, and never force-push. If no source changed, perform no empty commit
or push. Refresh the PR and record its current head on the workflow root as
`delivery.head_sha`; a new head deliberately invalidates old CI and CodeRabbit
evidence for the next loop check.

For every valid finding mapped in the durable handoff artifact, resolve a
thread only when the refreshed `published_head` is exactly equal to the
artifact's `tested_commit`. Record both heads and the equality result in the
same artifact. If the push or head refresh fails, is unavailable, or produces a
different head, record the mismatch, keep every mapped thread open, and let the
next Formula iteration inspect and retest that exact refreshed head. Commit
containment alone is not sufficient to resolve a thread.

Close with `gc.outcome=pass`. Do not merge or invoke provider-native
subagents.
