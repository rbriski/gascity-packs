Run one bounded current-head external-review iteration.

The children inspect evidence, resolve valid findings, rerun exact local gates,
publish fixes, and update the living report. The loop check then evaluates
required CI, CodeRabbit completion, all live unresolved review threads, human
change requests, PR/draft state, and head stability.

Never weaken a gate or resolve a thread unless the durable handoff proves
`published_head` is exactly equal to `tested_commit`; commit containment or a
pushed fix alone is not sufficient. The mechanical check owns the terminal
decision. Do not invoke provider-native subagents.
