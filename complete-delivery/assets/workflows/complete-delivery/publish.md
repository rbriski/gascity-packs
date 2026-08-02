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
duplicate. Before `gc.outcome=pass`, run `delivery-pr-open.sh` and require it
to prove the PR is open and non-draft, its head equals `delivery.head_sha`, its
GitHub `head.ref` equals the nonempty recorded `delivery.branch`, its
base equals `gc.var.base_branch`, and its repository, number, and URL exactly
equal `delivery.repo`, `delivery.pr_number`, and `delivery.pr_url`. Never
bypass branch protection or claim external review is green. The graph check
independently repeats this identity and exact-head validation.

Do not invoke provider-native subagents.
