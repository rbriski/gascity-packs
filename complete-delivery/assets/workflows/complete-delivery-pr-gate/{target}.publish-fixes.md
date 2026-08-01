Publish this iteration's verified review fixes.

Read `<artifact_root>/delivery/external-review-handoff.json`; require its local
gates to have passed for its recorded exact commit before publishing. Commit
only intentional changes with a focused message, push normally to the existing
PR branch, and never force-push. If no source changed, perform no empty commit
or push. Refresh the PR and record its current head on the workflow root as
`delivery.head_sha`; a new head deliberately invalidates old CI and CodeRabbit
evidence for the next loop check.

For every valid finding mapped in the durable handoff artifact, confirm the
refreshed PR head contains its fix commit before resolving that thread. Record
the published head and containment result in the same artifact. Keep the
thread open if the push or head-containment check fails, is unavailable, or
does not prove the published head contains the fix.

Close with `gc.outcome=pass`. Do not merge or invoke provider-native
subagents.
