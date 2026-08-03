#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

# The report stage is authority for the protected-merge transition. Revalidate
# the immutable deadline in this bounded Formula check before accepting it.
"$SCRIPT_DIR/delivery-external-review-deadline.sh" --validate

STATE="$(delivery_root_metadata delivery.report_state_path)"
[ -n "$STATE" ] || delivery_fail "delivery.report_state_path is missing"
STATE="$(delivery_resolve_path "$STATE")"
[ -s "$STATE" ] || delivery_fail "report state is missing or empty: $STATE"

HEAD_SHA="$(delivery_root_metadata delivery.head_sha)"
[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] || \
  delivery_fail "workflow root metadata delivery.head_sha must be a full lowercase 40-hex SHA"
REPO="$(delivery_root_metadata delivery.repo)"
[ -n "$REPO" ] || delivery_fail "workflow root metadata delivery.repo is missing"
PR_NUMBER="$(delivery_root_metadata delivery.pr_number)"
[[ "$PR_NUMBER" =~ ^[1-9][0-9]*$ ]] || \
  delivery_fail "workflow root metadata delivery.pr_number must be a positive integer"
PR_GATE="$(delivery_root_metadata delivery.pr_gate_path)"
[ -n "$PR_GATE" ] || delivery_fail "workflow root metadata delivery.pr_gate_path is missing"
PR_GATE="$(delivery_resolve_path "$PR_GATE")"
[ -f "$PR_GATE" ] && [ ! -L "$PR_GATE" ] || \
  delivery_fail "PR gate artifact is missing, not regular, or a symlink: $PR_GATE"

python3 - "$STATE" "$PR_GATE" "$HEAD_SHA" "$REPO" "$PR_NUMBER" "$DELIVERY_WORK_DIR" <<'PY'
import json
import os
import re
import sys

state_path, gate_path, head_sha, repo, pr_number, work_dir = sys.argv[1:]
try:
    with open(state_path, encoding="utf-8") as handle:
        state = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"cannot read report-green state: {exc}") from exc

try:
    with open(gate_path, encoding="utf-8") as handle:
        gate = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"cannot read PR gate artifact: {exc}") from exc

def require_full_lower_sha(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise SystemExit(f"{field} must be a full lowercase 40-hex SHA")
    return value

def require_list(value, field):
    if not isinstance(value, list):
        raise SystemExit(f"PR gate {field} must be a list")
    return value

if not isinstance(state, dict) or state.get("schema") != "gc.complete-delivery.report.v1":
    raise SystemExit("report state is not a gc.complete-delivery.report.v1 document")
if require_full_lower_sha(state.get("sha"), "report state sha") != head_sha:
    raise SystemExit("report state sha does not match workflow root delivery.head_sha")
stage = (state.get("stages") or {}).get("external-review")
if not isinstance(stage, dict) or stage.get("status") != "passed":
    raise SystemExit("report external-review stage is not durably passed")
if not isinstance(stage.get("summary"), str) or not stage["summary"].strip():
    raise SystemExit("report external-review stage has no summary")
evidence = stage.get("evidence")
if not isinstance(evidence, list) or not evidence:
    raise SystemExit("report external-review stage has no evidence")
if not any(
    isinstance(item, str)
    and os.path.normpath(item if os.path.isabs(item) else os.path.join(work_dir, item))
    == os.path.normpath(gate_path)
    for item in evidence
):
    raise SystemExit("report external-review evidence does not name the resolved PR gate artifact")

if not isinstance(gate, dict) or gate.get("schema") != "gc.complete-delivery.pr-gate.v1":
    raise SystemExit("PR gate artifact is not a gc.complete-delivery.pr-gate.v1 document")
if gate.get("repo") != repo:
    raise SystemExit("PR gate repository does not match workflow root delivery.repo")
if isinstance(gate.get("pr_number"), bool) or gate.get("pr_number") != int(pr_number):
    raise SystemExit("PR gate number does not match workflow root delivery.pr_number")
if require_full_lower_sha(gate.get("head_sha"), "PR gate head_sha") != head_sha:
    raise SystemExit("PR gate head_sha does not match workflow root delivery.head_sha")
if gate.get("passed") is not True or gate.get("state") != "passed":
    raise SystemExit("PR gate artifact is not a passing gate")
require_list(gate.get("required_checks"), "required_checks")
coderabbit = gate.get("coderabbit")
if not isinstance(coderabbit, dict):
    raise SystemExit("PR gate coderabbit must be an object")
for field in ("unresolved_threads", "human_change_requests", "blockers"):
    if require_list(gate.get(field), field):
        raise SystemExit(f"PR gate {field} is not empty")
if (
    isinstance(coderabbit.get("unresolved_threads"), bool)
    or coderabbit.get("unresolved_threads") != 0
    or require_list(coderabbit.get("active_change_requests"), "coderabbit.active_change_requests")
):
    raise SystemExit("PR gate coderabbit live review evidence is not empty")
if state.get("next_action") != "Proceed to protected merge.":
    raise SystemExit("report next action is not the canonical protected-merge action")
PY

echo "complete-delivery green external-review report is durable and ready for protected merge"
