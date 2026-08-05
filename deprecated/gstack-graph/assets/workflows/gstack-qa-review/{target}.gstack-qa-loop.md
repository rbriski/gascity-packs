Run the gstack QA loop.

This loop adapts upstream qa into browser QA, regression-test evidence, fix
application, and synthesis lanes. It proves the product behavior, not just the
diff.

The synthesize-qa lane owns `code_review.verdict=done|iterate` for the loop.

Do not invoke provider-native subagents. Continue only through this Gas City
graph loop.
