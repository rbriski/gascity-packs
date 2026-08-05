Apply gstack QA findings.

Use implementation target {{implementation_target}} for fixes. Fix behavior
defects first, then add or update regression tests. If the QA lane found only
missing evidence, run and record the missing proof instead of changing code.

Write a QA fix artifact under the artifact root.

Close with `gc.outcome=pass` and
`gstack.qa.fix_output_path=<QA fix artifact path>`.

Do not invoke provider-native subagents. This Gas City graph lane is the QA fix
delegation mechanism.
