Run the gstack regression-test evidence lane.

For each QA defect or important acceptance criterion, verify there is a focused
regression test or a clear reason it cannot be automated yet. Check that the
proof commands are repeatable from the repository.

Write findings under the artifact root.

Close with `gc.outcome=pass`,
`gstack.qa.regression_verdict=approve|iterate`, and
`gstack.qa.output_path=<regression report path>`.

Do not invoke provider-native subagents. You are the regression evidence lane.
