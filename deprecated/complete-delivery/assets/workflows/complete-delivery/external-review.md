Run the Complete Delivery current-head review expansion.

The internal gstack review is the normal review authority. The loop owns
current-head required checks, human review threads/change requests, valid fixes,
local regression, normal pushes, and living-report updates. CodeRabbit is
optional evidence only: when `coderabbit` is `off`, never request it, poll it,
wait for it, or treat its bot-only threads as blockers. Explicit `optional` or
`required` configuration re-enables only the configured observation gate.

Each Formula attempt is one frozen-candidate review/repair cycle. Freeze the
candidate head, gather all current findings before editing, and apply at most
one consolidated repair batch. After every push or canonical PR
head refresh, update `delivery.head_sha`, `delivery.repo`,
`delivery.branch`, `delivery.pr_number`, and `delivery.pr_url` from that
published head; invalidate prior gate evidence, rerun the full local gates, and
then rerun the current-head external gate. On first entry, record a single UTC
deadline no more than two hours ahead and preserve it unchanged across every
resolver, repair, publish, and wait attempt; stop with a non-pass outcome when
that deadline passes. The durable handoff may record a
passing local result only as `local_gates.status: "passed"` with a full
`tested_commit` exactly equal to the final published head. It passes only when
the mechanical gate reports that same current head green. This expansion is
bounded by the Formula's two-attempt resolve/test/publish loop: record
each exhausted external-review, repair, or publication attempt in
`gc.attempt_log`, and when either the two-attempt cap or the non-resettable UTC
deadline is exhausted close with a non-pass outcome rather than continuing to
wait or mutate.

Do not invoke provider-native subagents or perform work outside the expansion.
