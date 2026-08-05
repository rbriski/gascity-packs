Run the gstack document-release lane.

Check README, architecture docs, usage docs, changelog, and contributor docs
that are affected by the change. Write exact documentation updates or state why
no docs need to change.

Write findings under the artifact root.

Close with `gc.outcome=pass`,
`gstack.release.docs_verdict=approve|iterate`, and
`gstack.release.output_path=<document-release report path>`.

Do not invoke provider-native subagents. You are the docs lane.
