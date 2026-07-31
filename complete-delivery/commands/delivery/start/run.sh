#!/bin/sh
set -eu

if [ -z "${GC_PACK_DIR:-}" ]; then
  echo "gc complete-delivery delivery start: missing Gas City pack context" >&2
  exit 1
fi

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] || [ -z "${1:-}" ]; then
  cat "$GC_PACK_DIR/commands/delivery/start/help.md"
  [ -z "${1:-}" ] && exit 2 || exit 0
fi

BEAD_ID="$1"
shift
case "$BEAD_ID" in
  *[!A-Za-z0-9._-]*|"")
    echo "gc complete-delivery delivery start: invalid bead id: $BEAD_ID" >&2
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
    echo "gc complete-delivery delivery start: $1 requires a value" >&2
    exit 2
  }
}

while [ $# -gt 0 ]; do
  case "$1" in
    --rig) require_value "$@"; RIG="$2"; shift 2 ;;
    --rig=*) RIG="${1#--rig=}"; shift ;;
    --agent) require_value "$@"; AGENT="$2"; shift 2 ;;
    --agent=*) AGENT="${1#--agent=}"; shift ;;
    --artifact-root) require_value "$@"; ARTIFACT_ROOT="$2"; shift 2 ;;
    --artifact-root=*) ARTIFACT_ROOT="${1#--artifact-root=}"; shift ;;
    --interactive) INTERACTION_MODE="interactive"; REVIEW_MODE="interactive"; shift ;;
    --same-session) DRAIN_POLICY="same-session"; shift ;;
    --help|-h) cat "$GC_PACK_DIR/commands/delivery/start/help.md"; exit 0 ;;
    *)
      echo "gc complete-delivery delivery start: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

[ -n "$RIG" ] || {
  echo "gc complete-delivery delivery start: --rig <name> is required outside a rig session" >&2
  exit 2
}
case "$RIG" in
  -*|*[!A-Za-z0-9._-]*)
    echo "gc complete-delivery delivery start: invalid rig name: $RIG" >&2
    exit 2
    ;;
esac
case "$AGENT" in
  -*|*[!A-Za-z0-9._-]*)
    echo "gc complete-delivery delivery start: invalid agent target: $AGENT" >&2
    exit 2
    ;;
esac
[ -n "$ARTIFACT_ROOT" ] || ARTIFACT_ROOT="plans/complete-delivery/$BEAD_ID"
command -v gc >/dev/null 2>&1 || {
  echo "gc complete-delivery delivery start: gc is not on PATH" >&2
  exit 1
}

# Preserve the actual request as formula input.  The target can become a
# workflow root after sling, so downstream stages must not infer their goal
# from that root's title or from the repository checkout.
SOURCE_JSON="$(gc bd show "$BEAD_ID" --json)" || {
  echo "gc complete-delivery delivery start: cannot read source bead $BEAD_ID" >&2
  exit 1
}
SOURCE_TITLE="$(printf '%s' "$SOURCE_JSON" | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError as exc:
    raise SystemExit(f"source bead response is not valid JSON: {exc}")

if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
    raise SystemExit("source bead response must contain exactly one bead")

title = payload[0].get("title")
if not isinstance(title, str) or not title.strip():
    raise SystemExit("source bead has no usable title")
print(title.strip())
')" || {
  echo "gc complete-delivery delivery start: cannot resolve source intent for $BEAD_ID" >&2
  exit 1
}

exec gc sling "$RIG/$AGENT" "$BEAD_ID" --on complete-delivery \
  --var "artifact_root=$ARTIFACT_ROOT" \
  --var "source_bead_id=$BEAD_ID" \
  --var "source_title=$SOURCE_TITLE" \
  --var "report_title=$SOURCE_TITLE" \
  --var "interaction_mode=$INTERACTION_MODE" \
  --var "review_mode=$REVIEW_MODE" \
  --var "drain_policy=$DRAIN_POLICY" \
  --var "push=true" \
  --var "open_pr=true"
