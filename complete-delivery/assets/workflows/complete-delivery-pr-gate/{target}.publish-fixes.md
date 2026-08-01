Publish this iteration's verified review fixes.

Commit only intentional changes with a focused message, push normally to the
existing PR branch, and never force-push. If no source changed, perform no
empty commit or push. Refresh the PR and record its current head on the
workflow root as `delivery.head_sha`; a new head deliberately invalidates old
CI and CodeRabbit evidence for the next loop check.

Close with `gc.outcome=pass`. Do not merge or invoke provider-native
subagents.
