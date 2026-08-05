Terminal safety scope for publication, review, merge, deployment, and
production verification.

This scope is a fail-stop boundary. A non-pass outcome from any member aborts
the remaining release work, records the scope as failed, and preserves the
original failed control for explicit recovery and audit. Never rewrite a failed
outcome to unblock a release; resume only through an explicit new recovery
attempt that produces fresh passing evidence.
