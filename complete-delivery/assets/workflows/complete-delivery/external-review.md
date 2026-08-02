Run the Complete Delivery external-review expansion.

The loop owns current-head required checks, CodeRabbit completion and live
threads, human review threads/change requests, valid fixes, local regression,
normal pushes, and living-report updates. After every push or canonical PR
head refresh, update `delivery.head_sha`, `delivery.repo`,
`delivery.branch`, `delivery.pr_number`, and `delivery.pr_url` from that
published head; invalidate prior gate evidence, rerun the full local gates, and
then rerun the current-head external gate. The durable handoff may record a
passing local result only as `local_gates.status: "passed"` with a full
`tested_commit` exactly equal to the final published head. It passes only when
the mechanical gate reports that same current head green. This expansion is
already bounded by the Formula's 12-attempt resolve/test/publish loop: record
each exhausted external-review, repair, or publication attempt in
`gc.attempt_log`, and when that limit is exhausted close with a non-pass outcome
rather than continuing to wait or mutate.

Do not invoke provider-native subagents or perform work outside the expansion.
