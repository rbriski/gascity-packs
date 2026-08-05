Verify production behavior and exact-SHA attestation.

For `deploy_mode=ci`, do not replace or edit the deploy-stage evidence. The
graph check reopens it, re-queries GitHub, and requires an exact structured
match for repository, PR, merge SHA, configured workflow, run ID and URL,
successful conclusion, deployment ID/status, and configured environment before
it runs production verification.

For every real deployment mode (`command` or `ci`), require and run
`deploy_verify_command` with `DELIVERY_REPO=delivery.repo`,
`DELIVERY_PR=delivery.pr_number`, and `DELIVERY_SHA=delivery.merge_sha` under
its strictly positive, finite configured timeout of no more than one hour. The
command must consume or independently attest all three identity values and
prove the deployed revision is that exact SHA. Then run `smoke_command` with
the same delivery identity whenever `smoke_command` is nonblank, using the Formula's
strictly positive, finite `smoke_timeout` (no more than one hour). `allow_no_smoke=true`
permits only an omitted smoke command; it never suppresses a nonblank command.
When `allow_no_smoke=false`, a smoke command is required.
When `allow_no_smoke=true`,
require the nonempty `gc.var.no_smoke_reason`, record it on the workflow root
as `delivery.no_smoke_reason` using an argument-safe metadata update, and
record its SHA-256 label in verification evidence; it never waives deployment
verification or exact-SHA attestation. Treat either command timeout as a failed
verification attempt, record it in the verification evidence, and leave the
workflow blocked.
Capture the structured summary at `<artifact_root>/delivery/verify.log`, with
its recorded sibling stdout/stderr capture paths. The verification path must prove the
deployed revision is the merge SHA. When the service cannot expose its
revision, require provider metadata or another independently verifiable
artifact that binds the deployed target exactly to `delivery.merge_sha`; fail
closed if no such attestation is available. A local checkout SHA, successful
build, or HTTP 200 alone is never sufficient.

Diagnose deploy or application failures, record blocker evidence and
repository rollback guidance in `verify.log`, and leave the workflow blocked;
this Formula neither repairs and redeploys nor executes rollback within an
iteration. Do not roll forward to a different unreviewed SHA. Any rollback
must use a separately authorized repository-owned bounded workflow outside this
stage; do not declare the requested release complete here.

After proof for a real deployment, record `delivery.deployed_sha=<merge-sha>`,
`delivery.deploy_status=verified`, and `delivery.verify_evidence_path`. For an
explicit non-applicable artifact, preserve `delivery.deploy_status=not_applicable`,
omit `delivery.deployed_sha`, and record its documented reason plus nonempty
regular-file deployment evidence as the non-deployment attestation. This stage,
rather than release-readiness, owns the merge-SHA attestation: real deployments
require `delivery.deployed_sha == delivery.merge_sha`.
Before accepting evidence, canonicalize `delivery.deploy_evidence_path`,
`delivery.verify_evidence_path`, and real-deployment stdout/stderr capture
paths. Require every one to remain within `<artifact_root>/delivery`; reject
paths outside that directory, traversal, and symlink escapes, as well as empty
or nonregular deployment and verification evidence files.
Close with `gc.outcome=pass` only after that graph check: for `verified` it
reruns the configured verification and applicable smoke commands and compares
SHAs; for `not_applicable` it validates the reason and evidence instead of
running those commands.

Do not invoke provider-native subagents.
