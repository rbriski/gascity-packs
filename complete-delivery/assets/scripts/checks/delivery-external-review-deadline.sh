#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

MODE="${1:---initialize}"
case "$MODE" in
  --initialize|--validate) ;;
  *) delivery_fail "usage: delivery-external-review-deadline.sh [--initialize|--validate]" ;;
esac

STARTED_AT="$(delivery_root_metadata delivery.external_review_started_at)"
DEADLINE="$(delivery_root_metadata delivery.external_review_deadline)"
HISTORY="$(gc bd history "$DELIVERY_ROOT_ID" --json)" || \
  delivery_fail "cannot read workflow-root metadata history"
# This is a fail-closed production gate.  Do not accept a caller-provided
# clock: a stale/expired deadline must not become valid merely because a
# caller freezes time in its environment.
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ "$MODE" = "--initialize" ] && [ -z "$STARTED_AT" ] && [ -z "$DEADLINE" ]; then
  INITIAL_OUTPUT="$(python3 - "$NOW" "$HISTORY" <<'PY'
import datetime as dt
import json
import re
import sys

UTC = dt.timezone.utc
PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

def parse(value, field):
    if not isinstance(value, str) or not PATTERN.fullmatch(value):
        raise SystemExit(f"external-review {field} must be canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SystemExit(f"external-review {field} is malformed: {exc}")

now = parse(sys.argv[1], "current time")
try:
    history = json.loads(sys.argv[2])
except json.JSONDecodeError as exc:
    raise SystemExit(f"workflow-root metadata history is malformed: {exc}")
if not isinstance(history, list):
    raise SystemExit("workflow-root metadata history must be a list")
for item in history:
    metadata = item.get("Issue", {}).get("metadata", {}) if isinstance(item, dict) else {}
    if not isinstance(metadata, dict):
        raise SystemExit("workflow-root metadata history is malformed")
    if metadata.get("delivery.external_review_started_at") or metadata.get("delivery.external_review_deadline"):
        raise SystemExit("external-review deadline already has durable history but is missing from workflow-root metadata")
print(now.strftime("%Y-%m-%dT%H:%M:%SZ"))
print((now + dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
  )" || delivery_fail "cannot initialize external-review deadline"
  readarray -t INITIAL <<<"$INITIAL_OUTPUT"
  gc bd update "$DELIVERY_ROOT_ID" \
    --set-metadata "delivery.external_review_started_at=${INITIAL[0]}" \
    --set-metadata "delivery.external_review_deadline=${INITIAL[1]}" || \
    delivery_fail "cannot persist external-review deadline on workflow root"
  STARTED_AT="${INITIAL[0]}"
  DEADLINE="${INITIAL[1]}"
  HISTORY="$(gc bd history "$DELIVERY_ROOT_ID" --json)" || \
    delivery_fail "cannot re-read persisted workflow-root deadline"
fi

python3 - "$NOW" "$STARTED_AT" "$DEADLINE" "$HISTORY" <<'PY' || \
  delivery_fail "external-review deadline is missing, malformed, moved, or expired"
import datetime as dt
import json
import re
import sys

UTC = dt.timezone.utc
PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

def parse(value, field):
    if not isinstance(value, str) or not PATTERN.fullmatch(value):
        raise SystemExit(f"external-review {field} must be canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SystemExit(f"external-review {field} is malformed: {exc}")

now = parse(sys.argv[1], "current time")
started = parse(sys.argv[2], "started_at")
deadline = parse(sys.argv[3], "deadline")
if deadline <= started or deadline > started + dt.timedelta(hours=2):
    raise SystemExit("external-review deadline must be after entry and no later than two hours after entry")
if now >= deadline:
    raise SystemExit("external-review deadline has expired")
try:
    history = json.loads(sys.argv[4])
except json.JSONDecodeError as exc:
    raise SystemExit(f"workflow-root metadata history is malformed: {exc}")
if not isinstance(history, list):
    raise SystemExit("workflow-root metadata history must be a list")
recorded = None
for item in reversed(history):
    metadata = item.get("Issue", {}).get("metadata", {}) if isinstance(item, dict) else {}
    if not isinstance(metadata, dict):
        raise SystemExit("workflow-root metadata history is malformed")
    pair = (metadata.get("delivery.external_review_started_at"), metadata.get("delivery.external_review_deadline"))
    if pair == (None, None) or pair == ("", ""):
        continue
    if not pair[0] or not pair[1]:
        raise SystemExit("external-review deadline history is partially missing")
    pair_started = parse(pair[0], "history started_at")
    pair_deadline = parse(pair[1], "history deadline")
    if pair_deadline <= pair_started or pair_deadline > pair_started + dt.timedelta(hours=2):
        raise SystemExit("external-review deadline history exceeds two-hour bound")
    if recorded is None:
        recorded = pair
    elif pair != recorded:
        raise SystemExit("external-review deadline was reset or moved forward")
if recorded is None or recorded != (sys.argv[2], sys.argv[3]):
    raise SystemExit("external-review deadline current value does not match immutable first entry")
PY

echo "complete-delivery external-review deadline valid: $DEADLINE"
