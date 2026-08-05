Update the living report from the verified GitHub merge.

Mark stage `merge` as `passed`, store `delivery.merge_sha` as the report SHA,
and include the PR URL and merge evidence path. The next action is to deploy
that exact SHA. Run `report_publish_command` with `DELIVERY_REPORT_DIR` when
configured.

Close with `gc.outcome=pass`. Do not invoke provider-native subagents.
