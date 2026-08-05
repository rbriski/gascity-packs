Drain the gstack implementation convoy in one shared session.

Assigned implementation work should route to {{implementation_target}}. Use
this path only when `drain_policy` selects same-session execution. The item
lane remains single-lane so first-time factory users can follow the work
without hidden parallelism.

Do not edit source files from this control bead. The shared drain item owns
source changes and proof.

Close with `gc.outcome=pass` once the drain control work is complete.

Do not invoke provider-native subagents. The Gas City drain is the delegation
mechanism.
