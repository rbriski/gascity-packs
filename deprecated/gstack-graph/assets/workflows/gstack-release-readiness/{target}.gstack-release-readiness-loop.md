Run the gstack release readiness loop.

This loop adapts upstream document-release, ship, and land-and-deploy into
explicit Gas City lanes. It checks docs, PR readiness, deployment readiness,
and residual risk before finalization.

The synthesize-release-readiness lane owns `code_review.verdict=done|iterate`
for this readiness loop.

Do not invoke provider-native subagents. Continue only through this Gas City
graph loop.
