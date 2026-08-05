Optionally publish the gstack sprint.

Respect push {{push}} and open_pr {{open_pr}}. If neither is enabled, record a
no-op publish result. If publishing is enabled, verify the sprint report,
release readiness report, test evidence, and review approval are present before
any push or PR action.

Close with `gc.outcome=pass` and publish metadata.

Do not invoke provider-native subagents.
