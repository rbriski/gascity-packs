#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

REPO="$(delivery_root_metadata delivery.repo)"
PR_NUMBER="$(delivery_root_metadata delivery.pr_number)"
[ -n "$REPO" ] || delivery_fail "workflow root metadata delivery.repo is missing"
[ -n "$PR_NUMBER" ] || delivery_fail "workflow root metadata delivery.pr_number is missing"

REQUIRED_CHECKS="$(delivery_var required_checks auto)"
CODERABBIT="$(delivery_var coderabbit required)"
ALLOW_NO_CI="$(delivery_var allow_no_ci false)"
ARTIFACT_ROOT="$(delivery_var artifact_root "")"
[ -n "$ARTIFACT_ROOT" ] || delivery_fail "gc.var.artifact_root is missing"
ARTIFACT_ROOT="$(delivery_resolve_path "$ARTIFACT_ROOT")"
REPORT_PATH="$ARTIFACT_ROOT/delivery/pr-gate.json"

ARGS=(
  --repo "$REPO"
  --pr "$PR_NUMBER"
  --required-checks "$REQUIRED_CHECKS"
  --coderabbit "$CODERABBIT"
  --output "$REPORT_PATH"
)
if [ "$ALLOW_NO_CI" = "true" ]; then
  ARGS+=(--allow-no-ci)
fi

python3 "$SCRIPT_DIR/../delivery_gate.py" "${ARGS[@]}"
