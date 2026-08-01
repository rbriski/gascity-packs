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
HANDOFF_PATH="$ARTIFACT_ROOT/delivery/external-review-handoff.json"

validate_local_gate_handoff() {
  python3 - "$HANDOFF_PATH" <<'PY'
import json
import re
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        handoff = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"cannot read external-review handoff: {exc}")

if not isinstance(handoff, dict):
    raise SystemExit("external-review handoff must be an object")

tested_commit = handoff.get("tested_commit")
published_head = handoff.get("published_head")
local_gates = handoff.get("local_gates")
if not isinstance(tested_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", tested_commit):
    raise SystemExit("external-review handoff does not prove a full tested_commit")
if published_head != tested_commit:
    raise SystemExit("external-review handoff does not prove published_head == tested_commit")
if not isinstance(local_gates, dict):
    raise SystemExit("external-review handoff is missing local_gates evidence")
if local_gates.get("tested_commit") != tested_commit:
    raise SystemExit("local-gates evidence does not prove the recorded tested_commit")

def contains_disallowed_state(value):
    if isinstance(value, dict):
        return any(contains_disallowed_state(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_disallowed_state(item) for item in value)
    return isinstance(value, str) and value.lower() in {"blocked", "skipped"}

if contains_disallowed_state(local_gates):
    raise SystemExit("external-review handoff records blocked or skipped local gates")
if local_gates.get("status") != "passed":
    raise SystemExit("external-review handoff does not record passed local gates")

print(tested_commit.lower())
PY
}

TESTED_COMMIT="$(validate_local_gate_handoff)" || \
  delivery_fail "terminal approval requires proven passing local-gate evidence"

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

python3 - "$REPORT_PATH" "$TESTED_COMMIT" <<'PY' || \
  delivery_fail "terminal approval requires delivery_gate.py to validate the tested commit"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
if report.get("passed") is not True:
    raise SystemExit("delivery_gate.py did not report a passing gate")
if report.get("head_sha") != sys.argv[2]:
    raise SystemExit("delivery_gate.py evaluated a head other than the tested commit")
PY
