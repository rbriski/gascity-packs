This is the release scope's semantic boundary for the nested external-review
expansion. Read the nested scope outcome; a merely closed dependency is never
approval.

Close with `gc.outcome=pass` only when the nested `external-review` scope has
the explicit `gc.outcome=pass` result. If setup, the bounded review loop, or
the nested finalizer failed or exhausted, preserve that outcome and close this
step non-pass. Do not update the report, publish, merge, deploy, or rewrite
the nested scope outcome. The attached mechanical check independently verifies
the same outcome before it can release `report-green`.
