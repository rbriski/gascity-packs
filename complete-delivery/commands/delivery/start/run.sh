#!/bin/sh
set -eu

PREFIX="gc complete-delivery delivery start"
LOOKUP_TIMEOUT="${GC_COMPLETE_DELIVERY_LOOKUP_TIMEOUT:-15s}"
SLING_TIMEOUT="${GC_COMPLETE_DELIVERY_SLING_TIMEOUT:-30s}"

if [ -z "${GC_PACK_DIR:-}" ]; then
  echo "$PREFIX: missing Gas City pack context" >&2
  exit 1
fi

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] || [ -z "${1:-}" ]; then
  cat "$GC_PACK_DIR/commands/delivery/start/help.md"
  [ -z "${1:-}" ] && exit 2 || exit 0
fi

BEAD_ID="$1"
shift
case "$BEAD_ID" in
  -*|*[!A-Za-z0-9._-]*|"")
    echo "$PREFIX: invalid bead id: $BEAD_ID" >&2
    exit 2
    ;;
esac

RIG="${GC_RIG:-}"
AGENT="gc.run-operator"
ARTIFACT_ROOT=""
INTERACTION_MODE="autonomous"
REVIEW_MODE="agent"
DRAIN_POLICY="separate"

require_value() {
  [ "$#" -ge 2 ] && [ -n "$2" ] || {
    echo "$PREFIX: $1 requires a value" >&2
    exit 2
  }
}

require_equals_value() {
  [ -n "$2" ] || {
    echo "$PREFIX: $1 requires a value" >&2
    exit 2
  }
}

while [ $# -gt 0 ]; do
  case "$1" in
    --rig) require_value "$@"; RIG="$2"; shift 2 ;;
    --rig=*) value="${1#--rig=}"; require_equals_value --rig "$value"; RIG="$value"; shift ;;
    --agent) require_value "$@"; AGENT="$2"; shift 2 ;;
    --agent=*) value="${1#--agent=}"; require_equals_value --agent "$value"; AGENT="$value"; shift ;;
    --artifact-root) require_value "$@"; ARTIFACT_ROOT="$2"; shift 2 ;;
    --artifact-root=*)
      value="${1#--artifact-root=}"
      require_equals_value --artifact-root "$value"
      ARTIFACT_ROOT="$value"
      shift
      ;;
    --interactive) INTERACTION_MODE="interactive"; REVIEW_MODE="interactive"; shift ;;
    --same-session) DRAIN_POLICY="same-session"; shift ;;
    --help|-h) cat "$GC_PACK_DIR/commands/delivery/start/help.md"; exit 0 ;;
    *)
      echo "$PREFIX: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

[ -n "$RIG" ] || {
  echo "$PREFIX: --rig <name> is required outside a rig session" >&2
  exit 2
}
case "$RIG" in
  -*|*[!A-Za-z0-9._-]*|"")
    echo "$PREFIX: invalid rig name: $RIG" >&2
    exit 2
    ;;
esac
case "$AGENT" in
  -*|*[!A-Za-z0-9._-]*|"")
    echo "$PREFIX: invalid agent target: $AGENT" >&2
    exit 2
    ;;
esac
[ -n "$ARTIFACT_ROOT" ] || ARTIFACT_ROOT="plans/complete-delivery/$BEAD_ID"

for command_name in env gc python3 timeout mktemp; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "$PREFIX: $command_name is not on PATH" >&2
    exit 1
  }
done

LOOKUP_DIAGNOSTIC="$(mktemp "${TMPDIR:-/tmp}/complete-delivery-source.XXXXXX")" || {
  echo "$PREFIX: could not allocate source lookup diagnostics" >&2
  exit 1
}
trap 'rm -f "$LOOKUP_DIAGNOSTIC"' EXIT HUP INT TERM

# Preserve the actual request as Formula input. The target can become a
# workflow root after sling, so downstream stages must not infer their goal
# from that root's title or from the repository checkout.
if SOURCE_JSON="$(timeout --signal=KILL "$LOOKUP_TIMEOUT" gc bd show "$BEAD_ID" --json 2>"$LOOKUP_DIAGNOSTIC")"; then
  :
else
  status=$?
  if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
    echo "$PREFIX: gc bd show timed out after $LOOKUP_TIMEOUT for source $BEAD_ID" >&2
  else
    echo "$PREFIX: gc bd show failed with status $status for source $BEAD_ID" >&2
    [ ! -s "$LOOKUP_DIAGNOSTIC" ] || cat "$LOOKUP_DIAGNOSTIC" >&2
  fi
  exit 1
fi

SOURCE_TITLE="$(printf '%s' "$SOURCE_JSON" | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError as exc:
    raise SystemExit(f"source bead response is not valid JSON: {exc}")
if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
    raise SystemExit("source bead response must contain exactly one bead")
source = payload[0]
if source.get("id") != sys.argv[1]:
    raise SystemExit("source bead response ID does not match the requested bead")
title = source.get("title")
if not isinstance(title, str) or not title.strip():
    raise SystemExit("source bead has no usable title")
if any(ord(character) < 32 or ord(character) == 127 for character in title):
    raise SystemExit("source bead title contains control characters")
print(title.strip())
' "$BEAD_ID")" || {
  echo "$PREFIX: cannot resolve exact source intent for $BEAD_ID" >&2
  exit 1
}

# The Python preflight resolves the registered rig, validates the complete
# profile and artifact root without mutation, then atomically materializes the
# exact managed inventory. No workflow graph exists yet if it fails.
CONTROLLER_HOME="$(python3 "$GC_PACK_DIR/assets/scripts/prepare_delivery_launch.py" \
  --rig "$RIG" \
  --artifact-root "$ARTIFACT_ROOT")" || exit 1
case "$CONTROLLER_HOME" in
  /*) ;;
  *) echo "$PREFIX: launch preflight returned no canonical controller HOME" >&2; exit 1 ;;
esac

if timeout --signal=KILL "$SLING_TIMEOUT" env -u GC_HOME -u GC_PACK_DIR HOME="$CONTROLLER_HOME" \
  gc sling "$RIG/$AGENT" "$BEAD_ID" --on complete-delivery \
  --var "artifact_root=$ARTIFACT_ROOT" \
  --var "source_bead_id=$BEAD_ID" \
  --var "source_title=$SOURCE_TITLE" \
  --var "launcher_github_preflight=github-city-v1" \
  --var "report_title=$SOURCE_TITLE" \
  --var "interaction_mode=$INTERACTION_MODE" \
  --var "review_mode=$REVIEW_MODE" \
  --var "drain_policy=$DRAIN_POLICY" \
  --var "push=true" \
  --var "open_pr=true"
then
  exit 0
else
  status=$?
  if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
    echo "$PREFIX: gc sling timed out after $SLING_TIMEOUT; inspect durable bead state before retrying" >&2
  else
    echo "$PREFIX: gc sling failed with status $status; no successful dispatch was reported" >&2
  fi
  exit 1
fi
