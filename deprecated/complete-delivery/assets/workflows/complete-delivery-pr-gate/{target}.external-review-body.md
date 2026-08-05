This expansion's target is the terminal safety scope consumed by Complete
Delivery's outer `external-review` release member. Its outcome is
authoritative: a non-pass setup, bounded repair loop, or final review report
aborts this scope and cannot release `report-green`, merge, or deploy work.

Do not rewrite failed outcomes or convert a failure into a successful close.
Preserve the original check output and failure metadata for explicit,
auditable recovery.
