Update the existing living report from the canonical implementation summary.

Mark stage `implementation` as `passed`. Summarize owner-visible behavior,
name the canonical summary and changed-file evidence, and set the next action
to internal review, QA, and exact repository gates. Do not claim a test passed
unless the implementation evidence records it. Run `report_publish_command`
with `DELIVERY_REPORT_DIR` when configured.

Close with `gc.outcome=pass`. Do not invoke provider-native subagents.
