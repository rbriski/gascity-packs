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

python3 - "$STATE" <<'PY'
import json
import re
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"cannot read report-green state: {exc}") from exc

if not isinstance(state, dict) or state.get("schema") != "gc.complete-delivery.report.v1":
    raise SystemExit("report state is not a gc.complete-delivery.report.v1 document")
stage = (state.get("stages") or {}).get("external-review")
if not isinstance(stage, dict) or stage.get("status") != "passed":
    raise SystemExit("report external-review stage is not durably passed")
if not isinstance(stage.get("summary"), str) or not stage["summary"].strip():
    raise SystemExit("report external-review stage has no summary")
if not isinstance(stage.get("evidence"), list) or not stage["evidence"]:
    raise SystemExit("report external-review stage has no evidence")
next_action = state.get("next_action")
if not isinstance(next_action, str) or not re.search(r"\bprotected\s+merge\b", next_action, re.I):
    raise SystemExit("report next action does not require protected merge")
PY

echo "complete-delivery green external-review report is durable and ready for protected merge"
