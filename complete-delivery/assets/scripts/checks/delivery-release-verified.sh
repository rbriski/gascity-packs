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

delivery_command_is_nonblank() {
  [[ "$1" =~ [^[:space:]] ]]
}

delivery_command_label() {
  python3 - "$1" <<'PY'
import hashlib
import sys

print(f"sha256:{hashlib.sha256(sys.argv[1].encode()).hexdigest()}")
PY
}

delivery_value_sha256() {
  python3 - "$1" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
PY
}

delivery_run_bounded_command() {
  local name="$1"
  local command="$2"
  local timeout_value="$3"
  local status outcome label status_dir status_marker wrapper_status

  label="$(delivery_command_label "$command")"
  status_dir="$(mktemp -d "${TMPDIR:-/tmp}/delivery-command-status.XXXXXX")" || \
    delivery_fail "failed to create $name status directory"
  status_marker="$status_dir/status"

  # GNU timeout uses 124, 125, and 137 for its own outcomes, but a managed
  # command may legitimately return any of those statuses.  The inner Bash
  # writes a marker only after the strict managed command has returned; the
  # outer wrapper can therefore distinguish its own result from the child's.
  if timeout --kill-after=5s "$timeout_value" bash -u -o pipefail -c '
    set +e
    bash -euo pipefail -c "$1"
    child_status=$?
    if ! printf "%s\\n" "$child_status" >"$2/status.tmp"; then
      exit 125
    fi
    if ! mv "$2/status.tmp" "$2/status"; then
      exit 125
    fi
    exit "$child_status"
  ' delivery-bounded-command "$command" "$status_dir"; then
    wrapper_status=0
  else
    wrapper_status=$?
  fi

  if [ -f "$status_marker" ]; then
    status="$(<"$status_marker")"
    if [ "$status" -eq 0 ]; then
      outcome=passed
    else
      outcome=command_failure
    fi
  else
    status="$wrapper_status"
    case "$wrapper_status" in
      124|137) outcome=timeout ;;
      *) outcome=timeout_utility_failure ;;
    esac
  fi
  printf 'command=%s label=%s timeout=%s outcome=%s status=%s\n' \
    "$name" "$label" "$timeout_value" "$outcome" "$status" >>"$VERIFY_EVIDENCE" || \
    { rm -rf -- "$status_dir"; delivery_fail "failed to record $name verification evidence"; }
  rm -rf -- "$status_dir" || delivery_fail "failed to clean $name status directory"
  [ "$status" -eq 0 ]
}

delivery_run_deploy_command() {
  local artifact_root evidence_path evidence_tmp stdout_path stderr_path stdout_tmp stderr_tmp
  local status_dir status_marker command_label child_status wrapper_status outcome deploy_status

  MERGE_SHA="$(delivery_root_metadata delivery.merge_sha)"
  DEPLOY_COMMAND="$(delivery_var deploy_command '')"
  DEPLOY_TIMEOUT="$(delivery_var deploy_timeout 5m)"
  DELIVERY_REPO="$(delivery_root_metadata delivery.repo)"
  DELIVERY_PR="$(delivery_root_metadata delivery.pr_number)"
  artifact_root="$(delivery_var artifact_root '')"

  [ -n "$MERGE_SHA" ] || delivery_fail "delivery.merge_sha is missing"
  [[ "$MERGE_SHA" =~ ^[0-9a-f]{40}$ ]] || \
    delivery_fail "delivery.merge_sha is not a full lowercase Git SHA"
  delivery_command_is_nonblank "$DEPLOY_COMMAND" || \
    delivery_fail "deploy_command is required for deploy_mode=command"
  delivery_timeout_is_bounded "$DEPLOY_TIMEOUT" || \
    delivery_fail "deploy_timeout must be a positive finite duration no greater than 1h"
  [ -n "$DELIVERY_REPO" ] || delivery_fail "delivery.repo is missing"
  [ -n "$DELIVERY_PR" ] || delivery_fail "delivery.pr_number is missing"
  [ -n "$artifact_root" ] || delivery_fail "gc.var.artifact_root is missing"
  command -v timeout >/dev/null 2>&1 || delivery_fail "timeout is required on PATH"

  artifact_root="$(delivery_resolve_path "$artifact_root")"
  mkdir -p "$artifact_root/delivery" || delivery_fail "failed to create deployment evidence directory"
  evidence_path="$artifact_root/delivery/deploy.log"
  stdout_path="$artifact_root/delivery/deploy.stdout.log"
  stderr_path="$artifact_root/delivery/deploy.stderr.log"
  evidence_tmp="$(mktemp "$artifact_root/delivery/deploy.log.tmp.XXXXXX")" || \
    delivery_fail "failed to create deployment evidence file"
  stdout_tmp="$(mktemp "$artifact_root/delivery/deploy.stdout.log.tmp.XXXXXX")" || \
    { rm -f "$evidence_tmp"; delivery_fail "failed to create deployment stdout capture"; }
  stderr_tmp="$(mktemp "$artifact_root/delivery/deploy.stderr.log.tmp.XXXXXX")" || \
    { rm -f "$evidence_tmp" "$stdout_tmp"; delivery_fail "failed to create deployment stderr capture"; }
  status_dir="$(mktemp -d "${TMPDIR:-/tmp}/delivery-deploy-status.XXXXXX")" || \
    { rm -f "$evidence_tmp" "$stdout_tmp" "$stderr_tmp"; delivery_fail "failed to create deployment status directory"; }
  status_marker="$status_dir/status"
  command_label="$(delivery_command_label "$DEPLOY_COMMAND")"

  export DELIVERY_SHA="$MERGE_SHA" DELIVERY_REPO DELIVERY_PR
  if timeout --kill-after=5s "$DEPLOY_TIMEOUT" bash -u -o pipefail -c '
    set +e
    bash -euo pipefail -c "$1"
    child_status=$?
    if ! printf "%s\\n" "$child_status" >"$2/status.tmp"; then
      exit 125
    fi
    if ! mv "$2/status.tmp" "$2/status"; then
      exit 125
    fi
    exit "$child_status"
  ' delivery-deploy-command "$DEPLOY_COMMAND" "$status_dir" >"$stdout_tmp" 2>"$stderr_tmp"; then
    wrapper_status=0
  else
    wrapper_status=$?
  fi

  if [ -f "$status_marker" ]; then
    child_status="$(<"$status_marker")"
    if [ "$child_status" -eq 0 ]; then
      outcome=passed
      deploy_status=deployed
    else
      outcome=command_failure
      deploy_status=failed
    fi
  else
    child_status=unavailable
    case "$wrapper_status" in
      124|137) outcome=timeout ;;
      *) outcome=timeout_utility_failure ;;
    esac
    deploy_status=failed
  fi

  if ! mv "$stdout_tmp" "$stdout_path" || ! mv "$stderr_tmp" "$stderr_path"; then
    rm -f "$evidence_tmp" "$stdout_tmp" "$stderr_tmp"
    rm -rf -- "$status_dir"
    delivery_fail "failed to publish deployment command captures"
  fi
  printf '%s\n' \
    'schema=complete-delivery.deploy.v1' \
    "command_label=$command_label" \
    "timeout=$DEPLOY_TIMEOUT" \
    "outcome=$outcome" \
    "child_status=$child_status" \
    "wrapper_status=$wrapper_status" \
    "merge_sha=$MERGE_SHA" \
    "stdout_path=$stdout_path" \
    "stderr_path=$stderr_path" >"$evidence_tmp" || {
      rm -f "$evidence_tmp"
      rm -rf -- "$status_dir"
      delivery_fail "failed to write deployment evidence"
    }
  mv "$evidence_tmp" "$evidence_path" || {
    rm -f "$evidence_tmp"
    rm -rf -- "$status_dir"
    delivery_fail "failed to publish deployment evidence"
  }
  rm -rf -- "$status_dir" || delivery_fail "failed to clean deployment status directory"

  gc bd update "$DELIVERY_ROOT_ID" \
    --set-metadata "delivery.deploy_evidence_path=$evidence_path" \
    --set-metadata "delivery.deploy_command_label=$command_label" \
    --set-metadata "delivery.deploy_timeout=$DEPLOY_TIMEOUT" \
    --set-metadata "delivery.deploy_outcome=$outcome" \
    --set-metadata "delivery.deploy_child_status=$child_status" \
    --set-metadata "delivery.deploy_wrapper_status=$wrapper_status" \
    --set-metadata "delivery.deploy_merge_sha=$MERGE_SHA" \
    --set-metadata "delivery.deploy_stdout_path=$stdout_path" \
    --set-metadata "delivery.deploy_stderr_path=$stderr_path" \
    --set-metadata "delivery.deploy_status=$deploy_status" || \
    delivery_fail "failed to atomically record deployment evidence metadata"

  [ "$outcome" = passed ] && [ "$child_status" = 0 ] && [ "$wrapper_status" -eq 0 ] || \
    delivery_fail "deploy_command failed; see deployment evidence"
  echo "complete-delivery deploy command passed at $MERGE_SHA"
}

delivery_validate_command_deploy_evidence() {
  local evidence_path expected_label expected_timeout expected_sha expected_stdout expected_stderr
  evidence_path="$1"
  expected_label="$2"
  expected_timeout="$3"
  expected_sha="$4"
  expected_stdout="$5"
  expected_stderr="$6"
  python3 - "$evidence_path" "$expected_label" "$expected_timeout" "$expected_sha" "$expected_stdout" "$expected_stderr" <<'PY'
import sys

path, label, timeout, merge_sha, stdout_path, stderr_path = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        lines = [line.rstrip("\n") for line in handle]
except OSError as exc:
    raise SystemExit(f"cannot read deploy evidence: {exc}")

fields = {}
for line in lines:
    if "=" not in line:
        raise SystemExit("deploy evidence has a malformed line")
    key, value = line.split("=", 1)
    if not key or key in fields:
        raise SystemExit("deploy evidence has missing or duplicate fields")
    fields[key] = value

expected = {
    "schema": "complete-delivery.deploy.v1",
    "command_label": label,
    "timeout": timeout,
    "outcome": "passed",
    "child_status": "0",
    "wrapper_status": "0",
    "merge_sha": merge_sha,
    "stdout_path": stdout_path,
    "stderr_path": stderr_path,
}
if fields != expected:
    raise SystemExit("deploy evidence does not bind the current command, timeout, and merge SHA")
for capture_path in (stdout_path, stderr_path):
    try:
        with open(capture_path, "rb"):
            pass
    except OSError as exc:
        raise SystemExit(f"deploy evidence capture is missing: {exc}")
PY
}

MERGE_SHA="$(delivery_root_metadata delivery.merge_sha)"
DEPLOYED_SHA="$(delivery_root_metadata delivery.deployed_sha)"
DEPLOY_STATUS="$(delivery_root_metadata delivery.deploy_status)"
DEPLOY_EVIDENCE="$(delivery_root_metadata delivery.deploy_evidence_path)"
VERIFY_EVIDENCE="$(delivery_root_metadata delivery.verify_evidence_path)"
DEPLOY_MODE="$(delivery_var deploy_mode command)"
DEPLOY_COMMAND="$(delivery_var deploy_command "")"
DEPLOY_TIMEOUT="$(delivery_var deploy_timeout 5m)"
NA_REASON="$(delivery_var deploy_not_applicable_reason "")"
ALLOW_NO_SMOKE="$(delivery_var allow_no_smoke false)"
NO_SMOKE_REASON="$(delivery_var no_smoke_reason '')"

# This checker is shared by the deploy and production-verification graph
# steps. Empty metadata retains the formula's declared command default, while
# an unknown value must fail before either step can report success.
case "$DEPLOY_MODE" in
  "") DEPLOY_MODE=command ;;
  command|ci|not-applicable) ;;
  *) delivery_fail "deploy_mode must be command, ci, or not-applicable" ;;
esac

STEP_REF="$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.step_ref")"
case "$STEP_REF" in
  complete-delivery.deploy)
    [ "$DEPLOY_MODE" = command ] || {
      echo "complete-delivery deploy check requires agent-provided evidence for deploy_mode=$DEPLOY_MODE"
      exit 0
    }
    delivery_run_deploy_command
    exit 0
    ;;
  complete-delivery.verify-production) ;;
  "") delivery_fail "gc.step_ref is required for deployment lifecycle checks" ;;
  *) delivery_fail "unexpected deployment lifecycle gc.step_ref: $STEP_REF" ;;
esac

if [ "$ALLOW_NO_SMOKE" = "true" ] && ! [[ "$NO_SMOKE_REASON" =~ [^[:space:]] ]]; then
  delivery_fail "no_smoke_reason is required and must be nonblank when allow_no_smoke=true"
fi

[ -n "$MERGE_SHA" ] || delivery_fail "delivery.merge_sha is missing"
[[ "$MERGE_SHA" =~ ^[0-9a-f]{40}$ ]] || \
  delivery_fail "delivery.merge_sha is not a full lowercase Git SHA"
if [ "$DEPLOY_STATUS" = "not_applicable" ]; then
  [ "$DEPLOY_MODE" = "not-applicable" ] || \
    delivery_fail "not_applicable status is invalid for deploy_mode=$DEPLOY_MODE"
  delivery_command_is_nonblank "$NA_REASON" || \
    delivery_fail "not-applicable deployment requires a nonblank deploy_not_applicable_reason"
  [ -n "$DEPLOY_EVIDENCE" ] || delivery_fail "delivery.deploy_evidence_path is missing"
  DEPLOY_EVIDENCE="$(delivery_resolve_path "$DEPLOY_EVIDENCE")"
  if [ ! -f "$DEPLOY_EVIDENCE" ] || [ ! -s "$DEPLOY_EVIDENCE" ]; then
    delivery_fail "deploy evidence is missing, not a file, or empty: $DEPLOY_EVIDENCE"
  fi
  echo "complete-delivery deployment explicitly not applicable: $NA_REASON"
  exit 0
fi

DELIVERY_REPO="$(delivery_root_metadata delivery.repo)"
DELIVERY_PR="$(delivery_root_metadata delivery.pr_number)"
[ -n "$DELIVERY_REPO" ] || delivery_fail "delivery.repo is missing"
[ -n "$DELIVERY_PR" ] || delivery_fail "delivery.pr_number is missing"

case "$DEPLOY_MODE" in
  command)
    delivery_command_is_nonblank "$DEPLOY_COMMAND" || \
      delivery_fail "deploy_command is required for deploy_mode=command"
    delivery_timeout_is_bounded "$DEPLOY_TIMEOUT" || \
      delivery_fail "deploy_timeout must be a positive finite duration no greater than 1h"
    DEPLOY_COMMAND_LABEL="$(delivery_command_label "$DEPLOY_COMMAND")"
    DEPLOY_EVIDENCE_LABEL="$(delivery_root_metadata delivery.deploy_command_label)"
    DEPLOY_EVIDENCE_TIMEOUT="$(delivery_root_metadata delivery.deploy_timeout)"
    DEPLOY_OUTCOME="$(delivery_root_metadata delivery.deploy_outcome)"
    DEPLOY_CHILD_STATUS="$(delivery_root_metadata delivery.deploy_child_status)"
    DEPLOY_WRAPPER_STATUS="$(delivery_root_metadata delivery.deploy_wrapper_status)"
    DEPLOY_MERGE_SHA="$(delivery_root_metadata delivery.deploy_merge_sha)"
    DEPLOY_STDOUT="$(delivery_root_metadata delivery.deploy_stdout_path)"
    DEPLOY_STDERR="$(delivery_root_metadata delivery.deploy_stderr_path)"
    [ "$DEPLOY_EVIDENCE_LABEL" = "$DEPLOY_COMMAND_LABEL" ] || \
      delivery_fail "deploy evidence command label does not match deploy_command"
    [ "$DEPLOY_EVIDENCE_TIMEOUT" = "$DEPLOY_TIMEOUT" ] || \
      delivery_fail "deploy evidence timeout does not match deploy_timeout"
    [ "$DEPLOY_OUTCOME" = passed ] || delivery_fail "deploy evidence outcome must be passed"
    [ "$DEPLOY_CHILD_STATUS" = 0 ] || delivery_fail "deploy evidence child status must be 0"
    [ "$DEPLOY_WRAPPER_STATUS" = 0 ] || delivery_fail "deploy evidence wrapper status must be 0"
    [ "$DEPLOY_MERGE_SHA" = "$MERGE_SHA" ] || \
      delivery_fail "deploy evidence merge SHA does not match delivery.merge_sha"
    [ -n "$DEPLOY_STDOUT" ] && [ -n "$DEPLOY_STDERR" ] || \
      delivery_fail "deploy evidence capture paths are missing"
    delivery_validate_command_deploy_evidence "$DEPLOY_EVIDENCE" \
      "$DEPLOY_COMMAND_LABEL" "$DEPLOY_TIMEOUT" "$MERGE_SHA" \
      "$DEPLOY_STDOUT" "$DEPLOY_STDERR" || \
      delivery_fail "deploy evidence is forged, stale, or incomplete"
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
export DELIVERY_REPO DELIVERY_PR
cd "$DELIVERY_WORK_DIR"

VERIFY_COMMAND="$(delivery_var deploy_verify_command "")"
SMOKE_COMMAND="$(delivery_var smoke_command "")"
VERIFY_TIMEOUT="$(delivery_var deploy_verify_timeout 5m)"
SMOKE_TIMEOUT="$(delivery_var smoke_timeout 5m)"

delivery_command_is_nonblank "$VERIFY_COMMAND" || \
  delivery_fail "deploy_verify_command is required for deploy_mode=$DEPLOY_MODE"
command -v timeout >/dev/null 2>&1 || delivery_fail "timeout is required on PATH"
delivery_timeout_is_bounded "$VERIFY_TIMEOUT" || \
  delivery_fail "deploy_verify_timeout must be a positive finite duration no greater than 1h"

delivery_run_bounded_command deploy_verify "$VERIFY_COMMAND" "$VERIFY_TIMEOUT" || \
  delivery_fail "deploy_verify_command failed; see verification evidence"
if [ -n "$SMOKE_COMMAND" ]; then
  delivery_command_is_nonblank "$SMOKE_COMMAND" || \
    delivery_fail "smoke_command is required unless allow_no_smoke=true"
  delivery_timeout_is_bounded "$SMOKE_TIMEOUT" || \
    delivery_fail "smoke_timeout must be a positive finite duration no greater than 1h"
  delivery_run_bounded_command smoke "$SMOKE_COMMAND" "$SMOKE_TIMEOUT" || \
    delivery_fail "smoke_command failed; see verification evidence"
elif [ "$ALLOW_NO_SMOKE" != "true" ]; then
  delivery_fail "smoke_command is required unless allow_no_smoke=true"
else
  NO_SMOKE_REASON_SHA256="$(delivery_value_sha256 "$NO_SMOKE_REASON")"
  printf 'command=smoke outcome=not_run reason=allow_no_smoke_true no_smoke_reason_sha256=%s\n' "$NO_SMOKE_REASON_SHA256" >>"$VERIFY_EVIDENCE" || \
    delivery_fail "failed to record no-smoke verification exception"
fi

echo "complete-delivery release verified at $MERGE_SHA"
