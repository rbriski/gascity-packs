Publish the implementation branch and create or update its pull request.

Complete Delivery is intentionally armed: require `push=true` and
`open_pr=true`. Refuse to publish from `base_branch`, with a dirty uncommitted
result, or with untracked required work. Stage only intentional files, commit
with a focused message when needed, and push the current feature branch to its
normal fork/origin without force. Resolve the repository using `gh repo view`
rather than guessing its owner: read the exact `origin` URL and pass that URL
to `gh repo view`. Never let an ambient or upstream remote select another repo.

Create or update exactly one non-draft PR targeting `base_branch`. The body must
link the root bead and summarize behavior, local proof, review/QA evidence,
deployment plan, and the living report path.

After the push, resolve the canonical repository, current non-base branch, and
full remote head. Read all existing workflow-root publication identity before
choosing a PR operation. If any of `delivery.repo`, `delivery.branch`,
`delivery.head_sha`, `delivery.pr_number`, or `delivery.pr_url` is already
recorded, treat this as a revalidation: query the recorded PR when repository
and number are available, otherwise run `gh pr list --state open --head
<branch> --base <base_branch> --repo <repository>`. Require exactly one open
match, require every
recorded field to match the live PR and current full head, and update or ready
that same PR. Zero or multiple matches fail closed. A revalidation path must
never run `gh pr create`, even when prior identity is partial.

When no publication identity is recorded, first run `gh pr list --state open
--head <branch> --base <base_branch> --repo <repository>` and inspect its
structured JSON. With one match, adopt, update, and revalidate it. More than one
match is ambiguous and fails closed. Only the zero-match branch may run `gh pr
create --repo <repository>`, exactly once; immediately query the resulting PR
and require its live identity before writing metadata. This zero/one/many
decision is the idempotency boundary: never use a failed validation as
permission to open a replacement PR.

Atomically record the following complete workflow-root identity before closing:

- `delivery.repo`
- `delivery.branch`
- `delivery.head_sha`
- `delivery.pr_number`
- `delivery.pr_url`

Before `gc.outcome=pass`, run `delivery-pr-open.sh` and require it to prove the
PR is open and non-draft, its head equals `delivery.head_sha`, its GitHub
`head.ref` equals the nonempty recorded `delivery.branch`, its base equals
`gc.var.base_branch`, and its repository, number, and URL exactly equal
`delivery.repo`, `delivery.pr_number`, and `delivery.pr_url`. Never bypass
branch protection or claim external review is green. The graph check
independently repeats this identity and exact-head validation.

Do not invoke provider-native subagents.
