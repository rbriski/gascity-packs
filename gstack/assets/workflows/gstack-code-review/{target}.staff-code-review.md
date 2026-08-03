Run the gstack staff-engineer code review lane.

This lane is one bounded review pass. It is self-contained and must not invoke
the interactive standalone review workflow. Read the prepared review context,
the exact changed files, and relevant code outside the diff where values or
contracts require completeness checking. Apply this checklist:

1. correctness, control flow, error handling, and data integrity;
2. concurrency, transaction boundaries, idempotency, and retry behavior;
3. trust boundaries, including shell/SQL/path/URL injection and untrusted LLM
   output;
4. enum, status, tier, and type completeness across every sibling consumer;
5. API, schema, persistence, and backward-compatibility behavior;
6. acceptance-criteria, failure-path, boundary, and regression test coverage;
7. scope drift, promised-but-missing behavior, and unrelated changes; and
8. concrete maintainability or performance risks that can affect production.

Do not report speculation. Every blocking finding must include severity,
confidence, an exact file or artifact reference, the motivating code or
evidence, impact, and the smallest required fix. Separate non-blocking
follow-up clearly. An approval must state what was inspected and why no
required finding remains.

Write findings under the artifact root.

Close with `gc.outcome=pass`,
`code_review.staff_verdict=approve|iterate`, and
`code_review.output_path=<staff review report path>`.

Do not invoke provider-native subagents. You are the staff review lane.
