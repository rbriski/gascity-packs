#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

ARTIFACT_ROOT="$(delivery_var artifact_root '')"
REPORT="$(delivery_root_metadata delivery.report_path)"
STATE="$(delivery_root_metadata delivery.report_state_path)"
[ -n "$ARTIFACT_ROOT" ] || delivery_fail "gc.var.artifact_root is missing"
[ -n "$REPORT" ] || delivery_fail "delivery.report_path is missing"
[ -n "$STATE" ] || delivery_fail "delivery.report_state_path is missing"

# Bind both metadata paths to one exact, contained, non-symlink report bundle.
# The report tool subsequently proves that index.html and styles.css are the
# byte-for-byte rendering of this state document.
if ! STATE="$(python3 - "$DELIVERY_WORK_DIR" "$ARTIFACT_ROOT" "$REPORT" "$STATE" <<'PY'
import os
import stat
import sys
from pathlib import Path


def fail(message):
    raise SystemExit(message)


def relative_metadata(value, label, work):
    if not value or not value.strip() or value != value.strip():
        fail(f"{label} must be a nonblank path without edge whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        fail(f"{label} must not contain control characters")
    raw = Path(value)
    if ".." in raw.parts:
        fail(f"{label} must not contain parent traversal")
    if raw.is_absolute():
        try:
            relative = raw.relative_to(work)
        except ValueError:
            fail(f"{label} must be beneath the canonical delivery work directory")
    else:
        relative = raw
    if not relative.parts or relative.parts == (".",):
        fail(f"{label} must be a nonblank contained path")
    return relative


def require_no_symlinks(work, relative, label, final_kind):
    current = work
    for index, component in enumerate(relative.parts):
        current /= component
        try:
            details = os.lstat(current)
        except OSError as exc:
            fail(f"{label} is unavailable: {exc}")
        if stat.S_ISLNK(details.st_mode):
            fail(f"{label} contains a symlinked component")
        final = index == len(relative.parts) - 1
        if not final and not stat.S_ISDIR(details.st_mode):
            fail(f"{label} has a non-directory ancestor")
        if final and final_kind == "directory" and not stat.S_ISDIR(details.st_mode):
            fail(f"{label} is not a directory")
        if final and final_kind == "file":
            if not stat.S_ISREG(details.st_mode) or details.st_size == 0:
                fail(f"{label} is missing, not a regular file, or empty")
    try:
        current.resolve(strict=True).relative_to(work)
    except (OSError, ValueError):
        fail(f"{label} resolves outside the canonical delivery work directory")


work = Path(sys.argv[1]).resolve(strict=True)
if not work.is_dir():
    fail("canonical delivery work directory is not a directory")
artifact = relative_metadata(sys.argv[2], "gc.var.artifact_root", work)
report = relative_metadata(sys.argv[3], "delivery.report_path", work)
state = relative_metadata(sys.argv[4], "delivery.report_state_path", work)
expected_directory = artifact / "delivery-report"
if report != expected_directory / "index.html":
    fail("delivery.report_path must be exactly <artifact_root>/delivery-report/index.html")
if state != expected_directory / "state.json":
    fail("delivery.report_state_path must be exactly <artifact_root>/delivery-report/state.json")

require_no_symlinks(work, artifact, "gc.var.artifact_root", "directory")
require_no_symlinks(work, state, "delivery.report_state_path", "file")
require_no_symlinks(work, report, "delivery.report_path", "file")
require_no_symlinks(work, expected_directory / "styles.css", "report stylesheet", "file")
print(os.fspath(work / state))
PY
)"; then
  delivery_fail "report metadata must identify one canonical contained report bundle"
fi

MERGE_SHA="$(delivery_root_metadata delivery.merge_sha)"
DEPLOYED_SHA="$(delivery_root_metadata delivery.deployed_sha)"
DEPLOY_STATUS="$(delivery_root_metadata delivery.deploy_status)"
PR_URL="$(delivery_root_metadata delivery.pr_url)"
PRODUCTION_URL="$(delivery_var production_url '')"
SOURCE_ID="$(delivery_var source_bead_id '')"
SOURCE_TITLE="$(delivery_var source_title '')"
DEPLOY_MODE="$(delivery_var deploy_mode command)"
ALLOW_NO_SMOKE="$(delivery_var allow_no_smoke false)"
SMOKE_COMMAND="$(delivery_var smoke_command '')"
RECORDED_NO_SMOKE_REASON="$(delivery_root_metadata delivery.no_smoke_reason)"
EXPECTED_NO_SMOKE_REASON="$(delivery_var no_smoke_reason '')"

case "$DEPLOY_STATUS:$DEPLOY_MODE" in
  verified:command|verified:ci|not_applicable:not-applicable) ;;
  *)
    delivery_fail "delivery.deploy_status is inconsistent with gc.var.deploy_mode"
    ;;
esac

REPORT_ARGS=(
  validate
  --state "$STATE"
  --merge-sha "$MERGE_SHA"
  --deployed-sha "$DEPLOYED_SHA"
  --deploy-status "$DEPLOY_STATUS"
  --pr-url "$PR_URL"
  --production-url "$PRODUCTION_URL"
  --source-bead-id "$SOURCE_ID"
  --source-title "$SOURCE_TITLE"
)

if [ "$DEPLOY_STATUS" = "verified" ] && [ "$DEPLOY_MODE" != "not-applicable" ] && \
  [ "$ALLOW_NO_SMOKE" = "true" ] && ! [[ "$SMOKE_COMMAND" =~ [^[:space:]] ]]; then
  REPORT_ARGS+=(
    --require-no-smoke-reason
    --no-smoke-reason "$RECORDED_NO_SMOKE_REASON"
    --expected-no-smoke-reason "$EXPECTED_NO_SMOKE_REASON"
  )
elif [[ "$RECORDED_NO_SMOKE_REASON" =~ [^[:space:]] ]]; then
  delivery_fail "delivery.no_smoke_reason is stale because a smoke exception is not required"
fi

python3 "$SCRIPT_DIR/../delivery_report.py" "${REPORT_ARGS[@]}"

echo "complete-delivery living report valid: $STATE"
