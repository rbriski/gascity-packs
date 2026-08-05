Run the gstack CSO security review lane.

Use the upstream cso posture: threat model the changed surface, check OWASP
style web risks when relevant, check STRIDE categories, and keep noise low.
Only raise findings with concrete exploit or misuse scenarios.

Write findings under the artifact root.

Close with `gc.outcome=pass`,
`code_review.security_verdict=approve|iterate`, and
`code_review.output_path=<security review report path>`.

Do not invoke provider-native subagents. You are the security review lane.
