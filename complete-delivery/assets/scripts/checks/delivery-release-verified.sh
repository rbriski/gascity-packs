#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

delivery_timeout_is_bounded() {
  python3 - "$1" <<'PY'
import re
import sys
from decimal import Decimal, InvalidOperation

match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?|\.[0-9]+)([smhd]?)", sys.argv[1])
if not match:
    raise SystemExit(1)
try:
    value = Decimal(match.group(1))
except InvalidOperation:
    raise SystemExit(1)
seconds = value * {
    "": Decimal(1), "s": Decimal(1), "m": Decimal(60),
    "h": Decimal(3600), "d": Decimal(86400),
}[match.group(2)]
raise SystemExit(0 if Decimal(0) < seconds <= Decimal(3600) else 1)
PY
}

MERGE_SHA="$(delivery_root_metadata delivery.merge_sha)"
DEPLOYED_SHA="$(delivery_root_metadata delivery.deployed_sha)"
DEPLOY_STATUS="$(delivery_root_metadata delivery.deploy_status)"
DEPLOY_EVIDENCE="$(delivery_root_metadata delivery.deploy_evidence_path)"
VERIFY_EVIDENCE="$(delivery_root_metadata delivery.verify_evidence_path)"
DEPLOY_MODE="$(delivery_var deploy_mode command)"
DEPLOY_COMMAND="$(delivery_var deploy_command "")"
NA_REASON="$(delivery_var deploy_not_applicable_reason "")"

[ -n "$MERGE_SHA" ] || delivery_fail "delivery.merge_sha is missing"
[[ "$MERGE_SHA" =~ ^[0-9a-f]{40}$ ]] || \
  delivery_fail "delivery.merge_sha is not a full lowercase Git SHA"
if [ "$DEPLOY_STATUS" = "not_applicable" ]; then
  [ "$DEPLOY_MODE" = "not-applicable" ] || \
    delivery_fail "not_applicable status is invalid for deploy_mode=$DEPLOY_MODE"
  [ -n "$NA_REASON" ] || delivery_fail "not-applicable deployment requires deploy_not_applicable_reason"
  [ -n "$DEPLOY_EVIDENCE" ] || delivery_fail "delivery.deploy_evidence_path is missing"
  DEPLOY_EVIDENCE="$(delivery_resolve_path "$DEPLOY_EVIDENCE")"
  [ -s "$DEPLOY_EVIDENCE" ] || delivery_fail "deploy evidence is missing or empty: $DEPLOY_EVIDENCE"
  echo "complete-delivery deployment explicitly not applicable: $NA_REASON"
  exit 0
fi

case "$DEPLOY_MODE" in
  command)
    [ -n "$DEPLOY_COMMAND" ] || \
      delivery_fail "deploy_command is required for deploy_mode=command"
    ;;
  ci) ;;
  *) delivery_fail "deploy_mode must be command, ci, or not-applicable" ;;
esac

[ "$DEPLOY_STATUS" = "verified" ] || delivery_fail "delivery.deploy_status must be verified"
[ -n "$DEPLOYED_SHA" ] || delivery_fail "delivery.deployed_sha is missing"
[ "$DEPLOYED_SHA" = "$MERGE_SHA" ] || \
  delivery_fail "deployed SHA $DEPLOYED_SHA does not match merge SHA $MERGE_SHA"
[ -n "$DEPLOY_EVIDENCE" ] || delivery_fail "delivery.deploy_evidence_path is missing"
[ -n "$VERIFY_EVIDENCE" ] || delivery_fail "delivery.verify_evidence_path is missing"
DEPLOY_EVIDENCE="$(delivery_resolve_path "$DEPLOY_EVIDENCE")"
VERIFY_EVIDENCE="$(delivery_resolve_path "$VERIFY_EVIDENCE")"
[ -s "$DEPLOY_EVIDENCE" ] || delivery_fail "deploy evidence is missing or empty: $DEPLOY_EVIDENCE"
[ -s "$VERIFY_EVIDENCE" ] || delivery_fail "verification evidence is missing or empty: $VERIFY_EVIDENCE"

export DELIVERY_SHA="$MERGE_SHA"
DELIVERY_REPO="$(delivery_root_metadata delivery.repo)"
DELIVERY_PR="$(delivery_root_metadata delivery.pr_number)"
[ -n "$DELIVERY_REPO" ] || delivery_fail "delivery.repo is missing"
[ -n "$DELIVERY_PR" ] || delivery_fail "delivery.pr_number is missing"
export DELIVERY_REPO DELIVERY_PR
cd "$DELIVERY_WORK_DIR"

VERIFY_COMMAND="$(delivery_var deploy_verify_command "")"
SMOKE_COMMAND="$(delivery_var smoke_command "")"
ALLOW_NO_SMOKE="$(delivery_var allow_no_smoke false)"
VERIFY_TIMEOUT="$(delivery_var deploy_verify_timeout 5m)"
SMOKE_TIMEOUT="$(delivery_var smoke_timeout 5m)"

[ -n "$VERIFY_COMMAND" ] || \
  delivery_fail "deploy_verify_command is required for deploy_mode=$DEPLOY_MODE"
command -v timeout >/dev/null 2>&1 || delivery_fail "timeout is required on PATH"
delivery_timeout_is_bounded "$VERIFY_TIMEOUT" || \
  delivery_fail "deploy_verify_timeout must be a positive finite duration no greater than 1h"
delivery_timeout_is_bounded "$SMOKE_TIMEOUT" || \
  delivery_fail "smoke_timeout must be a positive finite duration no greater than 1h"

echo "complete-delivery deploy verification: $VERIFY_COMMAND"
timeout --kill-after=5s "$VERIFY_TIMEOUT" bash -lc "$VERIFY_COMMAND" || \
  delivery_fail "deploy_verify_command failed or timed out"
if [ -n "$SMOKE_COMMAND" ]; then
  echo "complete-delivery production smoke: $SMOKE_COMMAND"
  timeout --kill-after=5s "$SMOKE_TIMEOUT" bash -lc "$SMOKE_COMMAND" || \
    delivery_fail "smoke_command failed or timed out"
elif [ "$ALLOW_NO_SMOKE" != "true" ]; then
  delivery_fail "smoke_command is required unless allow_no_smoke=true"
fi

echo "complete-delivery release verified at $MERGE_SHA"
