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
print(result if isinstance(result, str) else "")
' "$2"
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

delivery_resolve_path() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$DELIVERY_WORK_DIR" "$1" ;;
  esac
}
