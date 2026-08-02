Publish the implementation branch and create or update its pull request.

Complete Delivery is intentionally armed: require `push=true` and
`open_pr=true`. Refuse to publish from `base_branch`, with a dirty uncommitted
result, or with untracked required work. Stage only intentional files, commit
with a focused message when needed, and push the current feature branch to its
normal fork/origin without force. Resolve the repository using `gh repo view`
rather than guessing its owner.

Create or update one non-draft PR targeting `base_branch`. The body must link
the root bead and summarize behavior, local proof, review/QA evidence,
deployment plan, and the living report path. Record these workflow-root
metadata fields before closing:

- `delivery.repo`
- `delivery.branch`
- `delivery.head_sha`
- `delivery.pr_number`
- `delivery.pr_url`

If a PR already exists for the branch, update it instead of opening a
duplicate. Never bypass branch protection or claim external review is green.
Close with `gc.outcome=pass`; the graph check independently verifies the open
PR and exact head.

Do not invoke provider-native subagents.
