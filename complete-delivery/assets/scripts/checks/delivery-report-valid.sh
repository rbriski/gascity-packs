#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

REPORT="$(delivery_root_metadata delivery.report_path)"
STATE="$(delivery_root_metadata delivery.report_state_path)"
[ -n "$REPORT" ] || delivery_fail "delivery.report_path is missing"
[ -n "$STATE" ] || delivery_fail "delivery.report_state_path is missing"
REPORT="$(delivery_resolve_path "$REPORT")"
STATE="$(delivery_resolve_path "$STATE")"
[ -s "$REPORT" ] || delivery_fail "HTML report is missing or empty: $REPORT"
[ -s "$STATE" ] || delivery_fail "report state is missing or empty: $STATE"
[ -s "$(dirname "$REPORT")/styles.css" ] || delivery_fail "report stylesheet is missing"

MERGE_SHA="$(delivery_root_metadata delivery.merge_sha)"
DEPLOYED_SHA="$(delivery_root_metadata delivery.deployed_sha)"
DEPLOY_STATUS="$(delivery_root_metadata delivery.deploy_status)"
PR_URL="$(delivery_root_metadata delivery.pr_url)"
PRODUCTION_URL="$(delivery_var production_url "")"

python3 "$SCRIPT_DIR/../delivery_report.py" validate \
  --state "$STATE" \
  --merge-sha "$MERGE_SHA" \
  --deployed-sha "$DEPLOYED_SHA" \
  --deploy-status "$DEPLOY_STATUS" \
  --pr-url "$PR_URL" \
  --production-url "$PRODUCTION_URL"

echo "complete-delivery living report valid: $REPORT"
