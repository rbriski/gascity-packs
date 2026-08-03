#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

# The report stage is authority for the protected-merge transition. Revalidate
# the immutable deadline in this bounded Formula check before accepting it.
"$SCRIPT_DIR/delivery-external-review-deadline.sh" --validate

ARTIFACT_ROOT="$(delivery_var artifact_root '')"
STATE="$(delivery_root_metadata delivery.report_state_path)"
[ -n "$ARTIFACT_ROOT" ] || delivery_fail "gc.var.artifact_root is missing"
[ -n "$STATE" ] || delivery_fail "delivery.report_state_path is missing"

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

# These artifacts become authority for a protected merge.  Metadata must name
# relative, in-worktree paths; accepting lexical normalization here would let a
# traversal, absolute path, or symlinked component substitute foreign evidence.
mapfile -t GREEN_ARTIFACTS < <(python3 - "$DELIVERY_WORK_DIR" "$ARTIFACT_ROOT" "$STATE" "$PR_GATE" <<'PY'
import os
import stat
import sys
from pathlib import Path


def fail(message):
    raise SystemExit(message)


def metadata_path(value, label):
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        fail(f"{label} must be a nonblank relative metadata path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        fail(f"{label} must not be absolute or contain traversal")
    return path


def nonsymlink_path(work_dir, relative, label):
    current = work_dir
    for component in relative.parts:
        if component in ("", "."):
            continue
        current /= component
        try:
            mode = os.lstat(current).st_mode
        except OSError as exc:
            fail(f"{label} is unavailable: {exc}")
        if stat.S_ISLNK(mode):
            fail(f"{label} contains a symlinked component: {current}")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(work_dir)
    except (OSError, ValueError) as exc:
        fail(f"{label} does not resolve within the canonical work directory: {exc}")
    return current, resolved


work_dir = Path(sys.argv[1]).resolve(strict=True)
artifact_root_value = metadata_path(sys.argv[2], "gc.var.artifact_root")
state_value = metadata_path(sys.argv[3], "delivery.report_state_path")
gate_value = metadata_path(sys.argv[4], "delivery.pr_gate_path")

artifact_root, artifact_root_canonical = nonsymlink_path(
    work_dir, artifact_root_value, "gc.var.artifact_root"
)
if not artifact_root.is_dir():
    fail("gc.var.artifact_root is not a directory")

report_directory = artifact_root / "delivery-report"
report_directory_canonical = artifact_root_canonical / "delivery-report"
state_path, state_canonical = nonsymlink_path(
    work_dir, state_value, "delivery.report_state_path"
)
try:
    state_canonical.relative_to(report_directory_canonical)
except ValueError:
    fail("delivery.report_state_path must resolve within the canonical artifact delivery-report directory")
if not stat.S_ISREG(os.lstat(state_path).st_mode) or state_path.stat().st_size == 0:
    fail("report state is missing, not a non-symlink regular file, or empty")

gate_path, gate_canonical = nonsymlink_path(
    work_dir, gate_value, "delivery.pr_gate_path"
)
expected_gate = artifact_root / "delivery" / "pr-gate.json"
expected_gate_canonical = artifact_root_canonical / "delivery" / "pr-gate.json"
if gate_path != expected_gate or gate_canonical != expected_gate_canonical:
    fail("delivery.pr_gate_path must be exactly the canonical artifact delivery/pr-gate.json path")
if not stat.S_ISREG(os.lstat(gate_path).st_mode):
    fail("PR gate artifact is missing or not a non-symlink regular file")

print(state_canonical)
print(gate_canonical)
PY
) || delivery_fail "report-green authority artifacts must be canonical, contained, and non-symlinked"

[ "${#GREEN_ARTIFACTS[@]}" -eq 2 ] || \
  delivery_fail "report-green authority artifact resolution returned an invalid result"
STATE="${GREEN_ARTIFACTS[0]}"
PR_GATE="${GREEN_ARTIFACTS[1]}"

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
