Update the living report after internal review, QA, and local command gates.

Read the approved review report, QA summary, and local-gate summary. Mark
stages `review`, `qa`, and `local-gates` as `passed` with their exact artifact
paths as evidence. Set the next action to release-readiness and pull-request
publication. If any prerequisite lacks approved evidence, do not paper over
it; fail this step so the graph can repair the missing stage.

Run `report_publish_command` with `DELIVERY_REPORT_DIR` when configured. Close
with `gc.outcome=pass`. Do not invoke provider-native subagents.
