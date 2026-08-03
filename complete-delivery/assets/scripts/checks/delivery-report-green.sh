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

# These artifacts become authority for a protected merge.  Open every
# directory and authority document through stable no-follow descriptors and
# parse the documents from those descriptors.  Checking paths and reopening
# them later would leave a replacement window for foreign evidence.
python3 - "$DELIVERY_WORK_DIR" "$ARTIFACT_ROOT" "$STATE" "$PR_GATE" "$HEAD_SHA" "$REPO" "$PR_NUMBER" <<'PY'
import json
import os
import re
import stat
import sys
from pathlib import Path


def fail(message):
    raise SystemExit(message)


def metadata_path(value, label, work_dir):
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        fail(f"{label} must be a nonblank metadata path")
    path = Path(value)
    if ".." in path.parts:
        fail(f"{label} must not contain traversal")
    if not path.is_absolute():
        if not path.parts or path.parts == (".",):
            fail(f"{label} must be a nonblank metadata path")
        return path

    # Persisted workflow metadata may use an absolute path.  Accept it only
    # when both its resolved target and its lexical components are contained
    # by the canonical work directory.  The latter keeps a symlinked input
    # from being normalized away before no-follow traversal rejects it.
    try:
        path.resolve(strict=False).relative_to(work_dir)
        relative = path.relative_to(work_dir)
    except ValueError:
        fail(f"{label} must resolve beneath the canonical work directory")
    if not relative.parts or relative.parts == (".",):
        fail(f"{label} must be a nonblank metadata path")
    return relative


def require_directory(fd, label):
    try:
        mode = os.fstat(fd).st_mode
    except OSError as exc:
        fail(f"{label} is unavailable: {exc}")
    if not stat.S_ISDIR(mode):
        fail(f"{label} is not a directory")


def open_directory(parent_fd, parts, label):
    fd = os.dup(parent_fd)
    try:
        for component in parts:
            try:
                child_fd = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            except OSError as exc:
                fail(f"{label} is unavailable or contains a symlinked component: {exc}")
            os.close(fd)
            fd = child_fd
            require_directory(fd, label)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_regular(parent_fd, name, label, require_nonempty=False):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        fail(f"{label} is unavailable or a symlink: {exc}")
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or (require_nonempty and details.st_size == 0):
            fail(f"{label} is missing, not a non-symlink regular file, or empty")
        return fd
    except BaseException:
        os.close(fd)
        raise


def parse_json(fd, label):
    try:
        # Retain the caller-owned descriptor for its cleanup path.  The dup
        # references the same already-open authority object, so this still
        # cannot follow a replacement pathname between validation and parse.
        with os.fdopen(os.dup(fd), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read {label}: {exc}") from exc


work_dir = Path(sys.argv[1]).resolve(strict=True)
artifact_root_value = metadata_path(sys.argv[2], "gc.var.artifact_root", work_dir)
state_value = metadata_path(sys.argv[3], "delivery.report_state_path", work_dir)
gate_value = metadata_path(sys.argv[4], "delivery.pr_gate_path", work_dir)
head_sha, repo, pr_number = sys.argv[5:]
artifact_parts = artifact_root_value.parts
state_parts = state_value.parts
gate_parts = gate_value.parts

if state_parts != artifact_parts + ("delivery-report", "state.json"):
    fail("delivery.report_state_path must be exactly the canonical artifact delivery-report/state.json path")
if gate_parts != artifact_parts + ("delivery", "pr-gate.json"):
    fail("delivery.pr_gate_path must be exactly the canonical artifact delivery/pr-gate.json path")

try:
    work_fd = os.open(work_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
except OSError as exc:
    fail(f"canonical work directory is unavailable: {exc}")

state_fd = gate_fd = -1
try:
    require_directory(work_fd, "canonical work directory")
    artifact_fd = open_directory(work_fd, artifact_parts, "gc.var.artifact_root")
    try:
        state_parent_fd = open_directory(
            artifact_fd, state_parts[len(artifact_parts) : -1], "delivery.report_state_path"
        )
        try:
            state_fd = open_regular(
                state_parent_fd, state_parts[-1], "report state", require_nonempty=True
            )
        finally:
            os.close(state_parent_fd)

        gate_parent_fd = open_directory(artifact_fd, ("delivery",), "delivery.pr_gate_path")
        try:
            gate_fd = open_regular(gate_parent_fd, "pr-gate.json", "PR gate artifact")
        finally:
            os.close(gate_parent_fd)
    finally:
        os.close(artifact_fd)

    state = parse_json(state_fd, "report-green state")
    gate = parse_json(gate_fd, "PR gate artifact")
finally:
    os.close(work_fd)
    if state_fd != -1:
        os.close(state_fd)
    if gate_fd != -1:
        os.close(gate_fd)

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
    and metadata_path(item, "report external-review evidence", work_dir).parts == gate_parts
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
