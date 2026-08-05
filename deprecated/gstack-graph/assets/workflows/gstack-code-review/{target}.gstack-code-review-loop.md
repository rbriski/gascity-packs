Run the gstack code-review loop.

This loop has staff-engineer, QA-evidence, CSO security, and gap-analysis
lanes, followed by synthesis and fix application. The apply-review-findings
lane owns `code_review.verdict=done|iterate` and
`code_review.report_path=<gstack review summary path>`.

The implementation review check repeats this loop until the latest verdict is
`done`.

Do not invoke provider-native subagents. Continue only through this Gas City
graph loop.
