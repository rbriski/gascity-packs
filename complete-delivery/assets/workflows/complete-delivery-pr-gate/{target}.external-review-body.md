Terminal safety scope for external review setup and the bounded frozen-head
loop.

A non-pass setup or loop outcome aborts the remaining review work and leaves
the expansion failed for Complete Delivery's parent release safety scope to
quarantine. Do not rewrite outcomes or silently continue from a closed failed
control; recovery must be a fresh, auditable attempt with current-head
evidence.
