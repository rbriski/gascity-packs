#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

command -v gh >/dev/null 2>&1 || delivery_fail "gh is required on PATH"
REPO="$(delivery_root_metadata delivery.repo)"
PR_NUMBER="$(delivery_root_metadata delivery.pr_number)"
RECORDED_SHA="$(delivery_root_metadata delivery.merge_sha)"
BASE_BRANCH="$(delivery_var base_branch '')"
[ -n "$REPO" ] || delivery_fail "workflow root metadata delivery.repo is missing"
[ -n "$PR_NUMBER" ] || delivery_fail "workflow root metadata delivery.pr_number is missing"
[ -n "$RECORDED_SHA" ] || delivery_fail "workflow root metadata delivery.merge_sha is missing"
[ -n "$BASE_BRANCH" ] || delivery_fail "configured base_branch is required"

PR_JSON="$(gh api "repos/$REPO/pulls/$PR_NUMBER")" || delivery_fail "failed to read PR $REPO#$PR_NUMBER"
RESULT="$(printf '%s' "$PR_JSON" | python3 -c '
import json
import sys
data = json.load(sys.stdin)
print("\t".join([
    str(bool(data.get("merged"))).lower(),
    str(data.get("merge_commit_sha") or ""),
    str((data.get("base") or {}).get("ref") or ""),
]))
')"
IFS=$'\t' read -r MERGED REMOTE_SHA BASE_REF <<<"$RESULT"
[ "$MERGED" = "true" ] || delivery_fail "PR $REPO#$PR_NUMBER is not merged"
[ "$REMOTE_SHA" = "$RECORDED_SHA" ] || \
  delivery_fail "recorded merge SHA $RECORDED_SHA does not match GitHub $REMOTE_SHA"
[ "$BASE_REF" = "$BASE_BRANCH" ] || \
  delivery_fail "GitHub PR base $BASE_REF does not match configured base_branch $BASE_BRANCH"

COMPARE="$(gh api "repos/$REPO/compare/$RECORDED_SHA...$BASE_BRANCH" --jq .status)" || \
  delivery_fail "could not verify merge SHA reachability from configured base_branch $BASE_BRANCH"
case "$COMPARE" in
  identical|ahead) ;;
  *) delivery_fail "merge SHA is not reachable from configured base_branch $BASE_BRANCH (compare=$COMPARE)" ;;
esac

echo "complete-delivery merge verified: $REPO#$PR_NUMBER -> $BASE_BRANCH @ $RECORDED_SHA"
