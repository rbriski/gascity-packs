Run the gstack gap-analysis review lane.

Use an adversarial investigate posture: compare requirements, plan, task
summaries, tests, and changed files. Look for work that was promised but not
implemented, behavior implemented without tests, and risks that should block
release readiness.

Write findings under the artifact root.

Close with `gc.outcome=pass`,
`code_review.gap_verdict=approve|iterate`, and
`code_review.output_path=<gap analysis report path>`.

Do not invoke provider-native subagents. You are the gap-analysis lane.
