Rerun the repository-native quality gates after review resolution.

Invoke `{{pack_root}}/assets/scripts/checks/delivery-local-gates.sh` with this claimed bead as
`GC_BEAD_ID`. Fix any new regression and repeat until every configured command
passes. Never push a review fix that fails the exact local gate sequence. If no
source changed because only remote checks are pending, still confirm the local
gate remains green.

Close with `gc.outcome=pass`. Do not invoke provider-native subagents.
