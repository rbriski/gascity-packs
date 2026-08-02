Verify production behavior and exact-SHA attestation.

For every real deployment mode (`command` or `ci`), require and run
`deploy_verify_command` with `DELIVERY_SHA=delivery.merge_sha` under its
strictly positive, finite configured timeout of no more than one hour; it must
prove the deployed revision is that exact SHA. Then run `smoke_command` under
the same bounded-timeout contract with the
same `DELIVERY_SHA` when `allow_no_smoke=false`. When `allow_no_smoke=true`,
record the explicit no-smoke exception instead; it never waives deployment
verification or exact-SHA attestation. Treat either command timeout as a failed
repair-redeploy-reverify attempt and record it in the verification evidence.
Capture full evidence at
`<artifact_root>/delivery/verify.log`. The verification path must prove the
deployed revision is the merge SHA; a local checkout SHA, a successful build,
or an HTTP 200 without revision evidence is insufficient when the service can
expose a revision.

Diagnose and repair deploy or application failures, redeploy the same intended
SHA when safe, and reverify. Make at most three complete
repair-redeploy-reverify attempts per Formula iteration. If a fourth repair
would be required, stop mutation, preserve blocker and rollback evidence in
the verification record, and close with a non-pass outcome. Do not roll forward
to a different unreviewed SHA. If rollback is required, execute the documented
repository path and leave the workflow blocked rather than declaring the
requested release complete.

After proof, record `delivery.deployed_sha=<merge-sha>`,
`delivery.deploy_status=verified`, and `delivery.verify_evidence_path`. For an
explicit non-applicable artifact, preserve `delivery.deploy_status=not_applicable`.
Close with `gc.outcome=pass`; the graph check reruns configured verification
and smoke commands and compares SHAs.

Do not invoke provider-native subagents.
