Synthesize the gstack release readiness result.

Read the document-release, ship-readiness, and deployment-readiness reports.
Set `code_review.verdict=done` only when release readiness is approved or every
remaining concern is explicitly non-blocking. Set `code_review.verdict=iterate`
when release-blocking issues remain.

Write one release readiness summary under the artifact root.

Close with `gc.outcome=pass`,
`code_review.verdict=done|iterate`,
`code_review.report_path=<release readiness summary path>`, and
`gstack.release.summary_path=<release readiness summary path>`.

Do not invoke provider-native subagents. Synthesis happens in this Gas City
fan-in lane.
