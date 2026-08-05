Run the gstack QA evidence review lane.

Check whether implementation proof covers the acceptance criteria and whether
there is enough evidence for the later qa stage to reproduce important flows.
Separate missing proof from actual behavior defects.

Write findings under the artifact root.

Close with `gc.outcome=pass`,
`code_review.qa_evidence_verdict=approve|iterate`, and
`code_review.output_path=<qa evidence report path>`.

Do not invoke provider-native subagents. You are the QA evidence lane.
