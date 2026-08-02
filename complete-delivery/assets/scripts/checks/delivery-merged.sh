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
RECORDED_HEAD="$(delivery_root_metadata delivery.head_sha)"
RECORDED_URL="$(delivery_root_metadata delivery.pr_url)"
BASE_BRANCH="$(delivery_var base_branch '')"
[ -n "$REPO" ] || delivery_fail "workflow root metadata delivery.repo is missing"
[ -n "$PR_NUMBER" ] || delivery_fail "workflow root metadata delivery.pr_number is missing"
[ -n "$RECORDED_SHA" ] || delivery_fail "workflow root metadata delivery.merge_sha is missing"
[ -n "$RECORDED_HEAD" ] || delivery_fail "workflow root metadata delivery.head_sha is missing"
[ -n "$RECORDED_URL" ] || delivery_fail "workflow root metadata delivery.pr_url is missing"
[ -n "$BASE_BRANCH" ] || delivery_fail "configured base_branch is required"

PR_JSON="$(gh api "repos/$REPO/pulls/$PR_NUMBER")" || delivery_fail "failed to read PR $REPO#$PR_NUMBER"
RESULT="$(printf '%s' "$PR_JSON" | python3 -c '
import json
import sys
data = json.load(sys.stdin)
merged = data.get("merged")
if not isinstance(merged, bool):
    raise ValueError("PR response has no boolean merged field")
print("\x1f".join([
    str(merged).lower(),
    str(data.get("state") or ""),
    str(data.get("merged_at") or ""),
    str(data.get("merge_commit_sha") or ""),
    str((data.get("head") or {}).get("sha") or ""),
    str((data.get("base") or {}).get("ref") or ""),
    str(data.get("html_url") or ""),
]))
')"
IFS=$'\x1f' read -r MERGED STATE MERGED_AT REMOTE_SHA REMOTE_HEAD BASE_REF PR_URL <<<"$RESULT"
[ "$MERGED" = "true" ] || delivery_fail "PR $REPO#$PR_NUMBER is not merged"
[ "$STATE" = "closed" ] || delivery_fail "merged PR $REPO#$PR_NUMBER is not closed (state=$STATE)"
[ -n "$MERGED_AT" ] || delivery_fail "merged PR $REPO#$PR_NUMBER has no merged_at timestamp"
[ "$REMOTE_SHA" = "$RECORDED_SHA" ] || \
  delivery_fail "recorded merge SHA $RECORDED_SHA does not match GitHub $REMOTE_SHA"
[ "$REMOTE_HEAD" = "$RECORDED_HEAD" ] || \
  delivery_fail "recorded head $RECORDED_HEAD does not match GitHub head $REMOTE_HEAD"
[ "$BASE_REF" = "$BASE_BRANCH" ] || \
  delivery_fail "GitHub PR base $BASE_REF does not match configured base_branch $BASE_BRANCH"
[ "$PR_URL" = "$RECORDED_URL" ] || \
  delivery_fail "GitHub PR URL $PR_URL does not match recorded URL $RECORDED_URL"

COMPARE="$(gh api "repos/$REPO/compare/$RECORDED_SHA...$BASE_BRANCH" --jq .status)" || \
  delivery_fail "could not verify merge SHA reachability from configured base_branch $BASE_BRANCH"
case "$COMPARE" in
  identical|ahead) ;;
  *) delivery_fail "merge SHA is not reachable from configured base_branch $BASE_BRANCH (compare=$COMPARE)" ;;
esac

echo "complete-delivery merge verified: $REPO#$PR_NUMBER -> $BASE_BRANCH @ $RECORDED_SHA"
