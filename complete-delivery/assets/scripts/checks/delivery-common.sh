#!/usr/bin/env bash

delivery_fail() {
  echo "complete-delivery-check: $*" >&2
  exit 1
}

delivery_metadata_value() {
  printf '%s' "$1" | python3 -c '
import json
import sys

key = sys.argv[1]
try:
    value = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)
if isinstance(value, list):
    value = value[0] if value else {}
metadata = value.get("metadata") if isinstance(value, dict) else {}
result = metadata.get(key, "") if isinstance(metadata, dict) else ""
if isinstance(result, str):
    print(result)
elif isinstance(result, bool):
    print("true" if result else "false")
elif isinstance(result, (int, float)):
    print(result)
else:
    print("")
' "$2"
}

delivery_bead_value() {
  printf '%s' "$1" | python3 -c '
import json
import sys

field = sys.argv[1]
try:
    value = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)
if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
    print("")
    raise SystemExit(0)
result = value[0].get(field, "")
print(result if isinstance(result, str) else "")
' "$2"
}

delivery_retry_lineage_is_valid() {
  python3 - "$DELIVERY_BEAD_ID" "$DELIVERY_ROOT_ID" "$DELIVERY_STEP_JSON" "$1" <<'PY'
import json
import re
import sys

attempt_id, root_id, attempt_json, control_json = sys.argv[1:]

def bead(raw, label):
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValueError(f"{label} must contain exactly one bead")
    return value[0]

def text(value):
    return value if isinstance(value, str) else ""

try:
    attempt = bead(attempt_json, "retry bead")
    control = bead(control_json, "control bead")
    metadata = attempt.get("metadata")
    control_metadata = control.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(control_metadata, dict):
        raise ValueError("retry and control beads must have metadata objects")
    control_id = text(metadata.get("gc.control_for"))
    step_id = text(metadata.get("gc.step_id"))
    step_ref = text(metadata.get("gc.step_ref"))
    attempt_number = text(metadata.get("gc.attempt"))
    if not re.fullmatch(r"[A-Za-z0-9._-]+", control_id):
        raise ValueError("retry gc.control_for must be one durable bead ID")
    if not re.fullmatch(r"[1-9][0-9]*", attempt_number):
        raise ValueError("retry gc.attempt must be a positive integer")
    if attempt.get("id") != attempt_id or control.get("id") != control_id:
        raise ValueError("retry gc.control_for does not resolve to its exact control bead")
    if not text(control.get("description")).strip():
        raise ValueError("control bead has no reusable logical contract")
    if text(metadata.get("gc.root_bead_id")) != root_id:
        raise ValueError("retry workflow root does not match the active workflow")
    if text(control_metadata.get("gc.root_bead_id")) != root_id:
        raise ValueError("control bead workflow root does not match the retry")
    if not step_id or text(control_metadata.get("gc.step_id")) != step_id:
        raise ValueError("control bead step does not match the retry")
    if text(metadata.get("gc.run_target")) != text(control_metadata.get("gc.run_target")):
        raise ValueError("control bead run target does not match the retry")
    if text(attempt.get("title")) != text(control.get("title")):
        raise ValueError("control bead title does not match the retry")
    control_ref = text(control_metadata.get("gc.step_ref"))
    if not control_ref or step_ref != f"{control_ref}.iteration.{attempt_number}":
        raise ValueError("retry step reference is not the control bead iteration")
    if text(metadata.get("gc.idempotency_key")) != f"{control_id}:attempt:{attempt_number}":
        raise ValueError("retry idempotency key does not bind the control bead and attempt")
except ValueError as exc:
    print(f"invalid blank-retry lineage: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

delivery_json_is_valid() {
  printf '%s' "$1" | python3 -c 'import json, sys; json.load(sys.stdin)' >/dev/null 2>&1
}

delivery_read_bead_json() {
  local bead_id="$1"
  local attempt=1
  local max_attempts=3
  local output status
  local diagnostic_file
  local -a diagnostics=()

  diagnostic_file="$(mktemp "${TMPDIR:-/tmp}/delivery-read-bead.XXXXXX")" || return 1

  # Lifecycle reads occasionally lose a transient Dolt connection.  Keep this
  # deliberately small and bounded: callers still fail closed after three
  # unsuccessful read-only attempts.
  while [ "$attempt" -le "$max_attempts" ]; do
    if [ -n "${DELIVERY_GC_TIMEOUT:-}" ]; then
      command -v timeout >/dev/null 2>&1 || {
        echo "complete-delivery-check: timeout is required for bounded gc bd show" >&2
        rm -f "$diagnostic_file"
        return 1
      }
      if output="$(timeout --signal=KILL "$DELIVERY_GC_TIMEOUT" gc bd show "$bead_id" --json 2>"$diagnostic_file")"; then
        status=0
      else
        status=$?
      fi
    else
      if output="$(gc bd show "$bead_id" --json 2>"$diagnostic_file")"; then
        status=0
      else
        status=$?
      fi
    fi
    if [ "$status" -eq 0 ]; then
      rm -f "$diagnostic_file"
      printf '%s' "$output"
      return 0
    fi

    # A timeout is a fail-closed condition, not a transient Dolt failure to
    # retry: another attempt would extend the caller's declared time budget.
    if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
      # The timeout's stderr is the only useful diagnostic for a bounded read;
      # retain it while removing the temporary file before returning failure.
      [ -s "$diagnostic_file" ] && cat "$diagnostic_file" >&2
      rm -f "$diagnostic_file"
      return 1
    fi

    diagnostics+=("$(<"$diagnostic_file")")

    if [ "$attempt" -lt "$max_attempts" ]; then
      sleep "0.$attempt"
    fi
    attempt=$((attempt + 1))
  done

  rm -f "$diagnostic_file"
  if [ -n "${diagnostics[$((max_attempts - 1))]}" ]; then
    printf '%s\n' "${diagnostics[$((max_attempts - 1))]}" >&2
  fi
  return 1
}

delivery_initialize_context() {
  DELIVERY_BEAD_ID="${GC_BEAD_ID:-}"
  [ -n "$DELIVERY_BEAD_ID" ] || delivery_fail "GC_BEAD_ID is required"
  command -v gc >/dev/null 2>&1 || delivery_fail "gc is required on PATH"
  command -v python3 >/dev/null 2>&1 || delivery_fail "python3 is required on PATH"

  DELIVERY_STEP_JSON="$(delivery_read_bead_json "$DELIVERY_BEAD_ID")" || \
    delivery_fail "gc bd show $DELIVERY_BEAD_ID failed"
  delivery_json_is_valid "$DELIVERY_STEP_JSON" || \
    delivery_fail "gc bd show $DELIVERY_BEAD_ID returned invalid JSON"
  DELIVERY_ROOT_ID="$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.root_bead_id")"
  [ -n "$DELIVERY_ROOT_ID" ] || DELIVERY_ROOT_ID="$DELIVERY_BEAD_ID"
  DELIVERY_ROOT_JSON="$DELIVERY_STEP_JSON"
  if [ "$DELIVERY_ROOT_ID" != "$DELIVERY_BEAD_ID" ]; then
    DELIVERY_ROOT_JSON="$(delivery_read_bead_json "$DELIVERY_ROOT_ID")" || \
      delivery_fail "gc bd show $DELIVERY_ROOT_ID failed"
    delivery_json_is_valid "$DELIVERY_ROOT_JSON" || \
      delivery_fail "gc bd show $DELIVERY_ROOT_ID returned invalid JSON"
  fi
  DELIVERY_LOGICAL_BEAD_ID="$DELIVERY_BEAD_ID"
  DELIVERY_LOGICAL_DESCRIPTION="$(delivery_bead_value "$DELIVERY_STEP_JSON" description)"
  DELIVERY_CONTROL_BEAD_ID="$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.control_for")"
  if ! [[ "$DELIVERY_LOGICAL_DESCRIPTION" =~ [^[:space:]] ]] && [ -n "$DELIVERY_CONTROL_BEAD_ID" ]; then
    [[ "$DELIVERY_CONTROL_BEAD_ID" =~ ^[A-Za-z0-9._-]+$ ]] || \
      delivery_fail "blank retry description has no valid gc.control_for bead ID"
    DELIVERY_CONTROL_JSON="$(delivery_read_bead_json "$DELIVERY_CONTROL_BEAD_ID")" || \
      delivery_fail "gc bd show $DELIVERY_CONTROL_BEAD_ID failed while recovering blank retry context"
    delivery_json_is_valid "$DELIVERY_CONTROL_JSON" || \
      delivery_fail "gc bd show $DELIVERY_CONTROL_BEAD_ID returned invalid JSON"
    delivery_retry_lineage_is_valid "$DELIVERY_CONTROL_JSON" || \
      delivery_fail "blank retry description has ambiguous or invalid logical lineage"
    DELIVERY_LOGICAL_BEAD_ID="$DELIVERY_CONTROL_BEAD_ID"
    DELIVERY_LOGICAL_DESCRIPTION="$(delivery_bead_value "$DELIVERY_CONTROL_JSON" description)"
  fi
  DELIVERY_WORK_DIR="${GC_WORK_DIR:-}"
  if [ -z "$DELIVERY_WORK_DIR" ]; then
    DELIVERY_WORK_DIR="$(delivery_metadata_value "$DELIVERY_ROOT_JSON" "gc.work_dir")"
  fi
  [ -n "$DELIVERY_WORK_DIR" ] || DELIVERY_WORK_DIR="$(pwd)"
}

delivery_root_metadata() {
  delivery_metadata_value "$DELIVERY_ROOT_JSON" "$1"
}

delivery_var() {
  local value
  value="$(delivery_root_metadata "gc.var.$1")"
  if [ -z "$value" ]; then
    value="$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.var.$1")"
  fi
  if [ -z "$value" ] && [ "$#" -gt 1 ]; then
    value="$2"
  fi
  printf '%s' "$value"
}

delivery_worker_preflight_evidence() {
  # The evidence is written only by the unrestricted first worker.  It binds
  # the attestation to this particular workflow root and logical preflight
  # control, so a value copied from another launch cannot satisfy Ralph's
  # restricted retry condition.
  printf 'github-worker-v1:%s:%s' "$DELIVERY_ROOT_ID" "$DELIVERY_LOGICAL_BEAD_ID"
}

delivery_worker_preflight_evidence_is_valid() {
  python3 - "$1" "$DELIVERY_ROOT_ID" "$DELIVERY_LOGICAL_BEAD_ID" <<'PY'
import sys

evidence, root_id, control_id = sys.argv[1:]
expected = f"github-worker-v1:{root_id}:{control_id}"
raise SystemExit(0 if evidence == expected else 1)
PY
}

delivery_is_preflight_control() {
  [ "$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.step_id")" = "delivery-preflight" ] || return 1
  [ "$(delivery_metadata_value "$DELIVERY_ROOT_JSON" "gc.formula_name")" = "complete-delivery" ] || return 1
}

delivery_resolve_path() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$DELIVERY_WORK_DIR" "$1" ;;
  esac
}
