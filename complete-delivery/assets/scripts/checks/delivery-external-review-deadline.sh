#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
command -v timeout >/dev/null 2>&1 || delivery_fail "timeout is required on PATH"
# Context has to be read before this gate can discover a durable deadline.
# Bound those bootstrap reads tightly; once a deadline is known, all later gc
# calls use its remaining time instead.
DELIVERY_GC_TIMEOUT=1s
delivery_initialize_context
unset DELIVERY_GC_TIMEOUT

MODE="${1:---validate}"
case "$MODE" in
  --initialize|--validate) ;;
  *) delivery_fail "usage: delivery-external-review-deadline.sh [--initialize|--validate]" ;;
esac

STARTED_AT="$(delivery_root_metadata delivery.external_review_started_at)"
DEADLINE="$(delivery_root_metadata delivery.external_review_deadline)"

delivery_refresh_root_metadata() {
  local timeout_value="$1"

  DELIVERY_GC_TIMEOUT="$timeout_value"
  DELIVERY_ROOT_JSON="$(delivery_read_bead_json "$DELIVERY_ROOT_ID")" || \
    delivery_fail "gc bd show $DELIVERY_ROOT_ID failed while refreshing external-review deadline"
  unset DELIVERY_GC_TIMEOUT
  delivery_json_is_valid "$DELIVERY_ROOT_JSON" || \
    delivery_fail "gc bd show $DELIVERY_ROOT_ID returned invalid JSON while refreshing external-review deadline"
  STARTED_AT="$(delivery_root_metadata delivery.external_review_started_at)"
  DEADLINE="$(delivery_root_metadata delivery.external_review_deadline)"
}

delivery_remaining_deadline_timeout() {
  python3 - "$DEADLINE" <<'PY'
import datetime as dt
import sys

try:
    deadline = dt.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
except ValueError:
    raise SystemExit("external-review deadline must be canonical UTC YYYY-MM-DDTHH:MM:SSZ")
# Subtract one full second so a command's timeout cannot extend beyond the
# immutable canonical-second deadline while the clock advances during launch.
remaining = int((deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()) - 1
if remaining <= 0:
    raise SystemExit("external-review deadline has expired")
print(f"{remaining}s")
PY
}

delivery_history_before_deadline() {
  local remaining
  remaining="$(delivery_remaining_deadline_timeout)" || return 1
  timeout --signal=KILL "$remaining" gc bd history "$DELIVERY_ROOT_ID" --json
}

# Concurrent setup lanes share a worktree.  Serialize only first-entry
# initialization, then re-read durable metadata while holding the lock so a
# waiter reuses the first persisted pair instead of overwriting it.
if [ "$MODE" = "--initialize" ] && [ -z "$STARTED_AT" ] && [ -z "$DEADLINE" ]; then
  command -v flock >/dev/null 2>&1 || \
    delivery_fail "flock is required on PATH to initialize an external-review deadline"
  # The lock holder performs two bounded metadata reads plus one durable
  # update. Thirty seconds covers loaded concurrent initialization without
  # turning a wedged data plane into an unbounded wait; lock exhaustion fails
  # closed below.
  DEADLINE_LOCK_WAIT=30
  DEADLINE_LOCK_DIR="$DELIVERY_WORK_DIR/.gc"
  mkdir -p "$DEADLINE_LOCK_DIR" || delivery_fail "cannot create external-review deadline lock directory"
  exec {deadline_lock_fd}>"$DEADLINE_LOCK_DIR/external-review-deadline-$DELIVERY_ROOT_ID.lock"
  flock -w "$DEADLINE_LOCK_WAIT" "$deadline_lock_fd" || \
    delivery_fail "cannot acquire external-review deadline initialization lock"
  delivery_refresh_root_metadata 1s
fi

if [ -n "$DEADLINE" ]; then
  known_timeout="$(delivery_remaining_deadline_timeout 2>&1)" || \
    delivery_fail "$known_timeout"
  HISTORY="$(timeout --signal=KILL "$known_timeout" gc bd history "$DELIVERY_ROOT_ID" --json)" || \
    delivery_fail "cannot read workflow-root metadata history before external-review deadline"
else
  HISTORY="$(timeout --signal=KILL 1s gc bd history "$DELIVERY_ROOT_ID" --json)" || \
    delivery_fail "cannot read workflow-root metadata history during deadline discovery"
fi

if [ "$MODE" = "--initialize" ] && [ -z "$STARTED_AT" ] && [ -z "$DEADLINE" ]; then
  # Read the real UTC clock only after the pre-persistence history read.  The
  # resulting pair is therefore not stale before it is made immutable.
  NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
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
  STARTED_AT="${INITIAL[0]}"
  DEADLINE="${INITIAL[1]}"
  gc_update_timeout="$(delivery_remaining_deadline_timeout)" || \
    delivery_fail "external-review deadline expired before persistence"
  timeout --signal=KILL "$gc_update_timeout" gc bd update "$DELIVERY_ROOT_ID" \
    --set-metadata "delivery.external_review_started_at=${INITIAL[0]}" \
    --set-metadata "delivery.external_review_deadline=${INITIAL[1]}" || \
    delivery_fail "cannot persist external-review deadline on workflow root before expiration"
  HISTORY="$(delivery_history_before_deadline)" || \
    delivery_fail "cannot re-read persisted workflow-root deadline before expiration"
fi

# This is a fail-closed production gate. Do not accept a caller-provided
# clock: a stale/expired deadline must not become valid merely because a
# caller freezes time in its environment. Recompute it after every internal
# read so validation cannot authorize work that crossed the deadline in-flight.
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
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
