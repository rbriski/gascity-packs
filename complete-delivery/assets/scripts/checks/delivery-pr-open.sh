#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

command -v gh >/dev/null 2>&1 || delivery_fail "gh is required on PATH"
REPO="$(delivery_root_metadata delivery.repo)"
PR_NUMBER="$(delivery_root_metadata delivery.pr_number)"
RECORDED_SHA="$(delivery_root_metadata delivery.head_sha)"
RECORDED_URL="$(delivery_root_metadata delivery.pr_url)"
RECORDED_BRANCH="$(delivery_root_metadata delivery.branch)"
BASE_BRANCH="$(delivery_var base_branch '')"
[ -n "$REPO" ] || delivery_fail "workflow root metadata delivery.repo is missing"
[ -n "$PR_NUMBER" ] || delivery_fail "workflow root metadata delivery.pr_number is missing"
[ -n "$RECORDED_SHA" ] || delivery_fail "workflow root metadata delivery.head_sha is missing"
[ -n "$RECORDED_URL" ] || delivery_fail "workflow root metadata delivery.pr_url is missing"
[ -n "$RECORDED_BRANCH" ] || delivery_fail "workflow root metadata delivery.branch is missing"
[ -n "$BASE_BRANCH" ] || delivery_fail "configured base_branch is required"

command -v timeout >/dev/null 2>&1 || delivery_fail "timeout is required on PATH"
PR_JSON="$(timeout --kill-after=5s 30s gh api "repos/$REPO/pulls/$PR_NUMBER")" || delivery_fail "failed to read PR $REPO#$PR_NUMBER"
RESULT="$(printf '%s' "$PR_JSON" | python3 -c '
import json
import sys
data = json.load(sys.stdin)
draft = data.get("draft")
if not isinstance(draft, bool):
    raise ValueError("PR response has no boolean draft field")
print("\t".join([
    str(data.get("state") or ""),
    str(draft).lower(),
    str((data.get("head") or {}).get("sha") or ""),
    str((data.get("head") or {}).get("ref") or ""),
    str((data.get("base") or {}).get("ref") or ""),
    str(((data.get("base") or {}).get("repo") or {}).get("full_name") or ""),
    str(data.get("number") or ""),
    str(data.get("html_url") or ""),
]))
')"
IFS=$'\t' read -r STATE DRAFT REMOTE_SHA REMOTE_BRANCH REMOTE_BASE REMOTE_REPO REMOTE_NUMBER PR_URL <<<"$RESULT"
[ "$STATE" = "open" ] || delivery_fail "PR is not open (state=$STATE)"
[ "$DRAFT" = "false" ] || delivery_fail "PR is still a draft"
[ "$REMOTE_SHA" = "$RECORDED_SHA" ] || \
  delivery_fail "recorded head $RECORDED_SHA does not match GitHub head $REMOTE_SHA"
[ "$REMOTE_BRANCH" = "$RECORDED_BRANCH" ] || \
  delivery_fail "recorded branch $RECORDED_BRANCH does not match GitHub head branch $REMOTE_BRANCH"
[ "$REMOTE_BASE" = "$BASE_BRANCH" ] || \
  delivery_fail "PR base $REMOTE_BASE does not match configured base branch $BASE_BRANCH"
[ "$REMOTE_REPO" = "$REPO" ] || \
  delivery_fail "PR repository $REMOTE_REPO does not match recorded repository $REPO"
[ "$REMOTE_NUMBER" = "$PR_NUMBER" ] || \
  delivery_fail "PR number $REMOTE_NUMBER does not match recorded PR number $PR_NUMBER"
[ "$PR_URL" = "$RECORDED_URL" ] || \
  delivery_fail "PR URL $PR_URL does not match recorded URL $RECORDED_URL"

echo "complete-delivery PR open and current: $PR_URL @ $REMOTE_SHA"
