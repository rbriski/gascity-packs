Apply gstack code-review findings.

Use implementation target {{implementation_target}} for any code changes. Read
the review synthesis from workflow root `gc.build.code_review_report_path`. If
all required review lanes approve, write a no-op review-fix artifact. If
required fixes or missing evidence remain, make the smallest focused changes
and run proof commands. Write the review-fix artifact under the artifact root
and record it on the workflow root as `gc.build.review_fix_summary_path`.

Set `code_review.verdict=done` only when staff, QA evidence, security, and gap
analysis approve, and update workflow root metadata with
`gc.build.code_review_status=approved`. Set `code_review.verdict=iterate` when
required fixes remain, and update workflow root metadata with
`gc.build.code_review_status=draft`.

Always close with `gc.outcome=pass`,
`code_review.verdict=done|iterate`,
`code_review.report_path=<review-fix artifact path>`, and
`code_review.output_path=<review-fix artifact path>`.

Do not invoke provider-native subagents. This Gas City graph lane is the delegation
mechanism for fixes.
