Synthesize the gstack QA result.

Read the browser QA, regression evidence, and QA fix artifacts. Set
`code_review.verdict=done` only when QA behavior and regression evidence are
approved. Set `code_review.verdict=iterate` when defects or missing evidence
remain.

Write one QA summary under the artifact root.

Close with `gc.outcome=pass`,
`code_review.verdict=done|iterate`,
`code_review.report_path=<QA summary path>`, and
`gstack.qa.summary_path=<QA summary path>`.

Do not invoke provider-native subagents. Synthesis happens in this Gas City
fan-in lane.
