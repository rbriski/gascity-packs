Run the gstack browser QA lane.

Exercise the most important user workflows with the available browser or
command-line testing surface. Capture screenshots, logs, command output, or
other evidence. If browser testing is unavailable, record the fallback proof
used and what remains untested.

Write findings under the artifact root.

Close with `gc.outcome=pass`,
`gstack.qa.browser_verdict=approve|iterate`, and
`gstack.qa.output_path=<browser QA report path>`.

Do not invoke provider-native subagents. You are the QA lane.
