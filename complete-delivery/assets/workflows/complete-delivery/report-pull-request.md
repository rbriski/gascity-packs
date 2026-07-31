Update the living report with the published pull request.

Read the verified `delivery.pr_url` and `delivery.head_sha`. Mark stage
`pull-request` as `passed`, include both as evidence, set the report PR URL,
and make the next action "Resolve required CI, CodeRabbit, and review
findings." Run `report_publish_command` with `DELIVERY_REPORT_DIR` when
configured.

Close with `gc.outcome=pass`. Do not invoke provider-native subagents.
