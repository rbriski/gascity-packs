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
[ -n "$REPO" ] || delivery_fail "workflow root metadata delivery.repo is missing"
[ -n "$PR_NUMBER" ] || delivery_fail "workflow root metadata delivery.pr_number is missing"
[ -n "$RECORDED_SHA" ] || delivery_fail "workflow root metadata delivery.head_sha is missing"

PR_JSON="$(gh api "repos/$REPO/pulls/$PR_NUMBER")" || delivery_fail "failed to read PR $REPO#$PR_NUMBER"
RESULT="$(printf '%s' "$PR_JSON" | python3 -c '
import json
import sys
data = json.load(sys.stdin)
print("\t".join([
    str(data.get("state") or ""),
    str(bool(data.get("draft"))).lower(),
    str((data.get("head") or {}).get("sha") or ""),
    str(data.get("html_url") or ""),
]))
')"
IFS=$'\t' read -r STATE DRAFT REMOTE_SHA PR_URL <<<"$RESULT"
[ "$STATE" = "open" ] || delivery_fail "PR is not open (state=$STATE)"
[ "$DRAFT" = "false" ] || delivery_fail "PR is still a draft"
[ "$REMOTE_SHA" = "$RECORDED_SHA" ] || \
  delivery_fail "recorded head $RECORDED_SHA does not match GitHub head $REMOTE_SHA"

echo "complete-delivery PR open and current: $PR_URL @ $REMOTE_SHA"
