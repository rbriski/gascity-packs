#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
command -v timeout >/dev/null 2>&1 || delivery_fail "timeout is required on PATH"

# A Beads read can take longer than the old one-second assumption on a real
# workflow root.  All reads remain bounded, and every mutation below is also
# bounded by the immutable deadline once it is known.
DELIVERY_GC_TIMEOUT=5s
delivery_initialize_context
unset DELIVERY_GC_TIMEOUT

MODE="${1:---validate}"
case "$MODE" in
  --initialize|--validate) ;;
  *) delivery_fail "usage: delivery-external-review-deadline.sh [--initialize|--validate]" ;;
esac

[[ "$DELIVERY_ROOT_ID" =~ ^[A-Za-z0-9._-]+$ ]] || \
  delivery_fail "workflow root ID is unsafe for deadline record"
DEADLINE_RECORD_DIR="$DELIVERY_WORK_DIR/.gc"
DEADLINE_RECORD="$DEADLINE_RECORD_DIR/external-review-deadline-$DELIVERY_ROOT_ID.json"
DEADLINE_IO_TIMEOUT=5s

STARTED_AT="$(delivery_root_metadata delivery.external_review_started_at)"
DEADLINE="$(delivery_root_metadata delivery.external_review_deadline)"

delivery_validate_deadline_pair() {
  python3 - "$1" "$2" <<'PY'
import datetime as dt
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

started = parse(sys.argv[1], "started_at")
deadline = parse(sys.argv[2], "deadline")
if deadline <= started or deadline > started + dt.timedelta(hours=2):
    raise SystemExit("external-review deadline must be after entry and no later than two hours after entry")
PY
}

delivery_read_deadline_record() {
  python3 - "$DEADLINE_RECORD" "$DELIVERY_ROOT_ID" <<'PY'
import datetime as dt
import json
import os
import re
import stat
import sys

path, root_id = sys.argv[1:]
try:
    info = os.lstat(path)
except FileNotFoundError:
    raise SystemExit("external-review deadline record is missing")
if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
    raise SystemExit("external-review deadline record must be one regular file")
try:
    with open(path, encoding="utf-8") as handle:
        record = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"external-review deadline record is invalid: {exc}")
if not isinstance(record, dict) or set(record) != {"version", "root_id", "started_at", "deadline"}:
    raise SystemExit("external-review deadline record has an invalid shape")
if record.get("version") != 1 or record.get("root_id") != root_id:
    raise SystemExit("external-review deadline record does not belong to this workflow root")
pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
for key in ("started_at", "deadline"):
    if not isinstance(record.get(key), str) or not pattern.fullmatch(record[key]):
        raise SystemExit(f"external-review deadline record {key} is not canonical UTC")
try:
    started = dt.datetime.strptime(record["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    deadline = dt.datetime.strptime(record["deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
except ValueError as exc:
    raise SystemExit(f"external-review deadline record is malformed: {exc}")
if deadline <= started or deadline > started + dt.timedelta(hours=2):
    raise SystemExit("external-review deadline record exceeds two-hour bound")
print(record["started_at"])
print(record["deadline"])
PY
}

delivery_create_deadline_record() {
  local started_at="$1"
  local deadline="$2"
  mkdir -p "$DEADLINE_RECORD_DIR" || return 1
  python3 - "$DEADLINE_RECORD" "$DELIVERY_ROOT_ID" "$started_at" "$deadline" <<'PY'
import datetime as dt
import json
import os
import sys

path, root_id, started_text, deadline_text = sys.argv[1:]
try:
    started = dt.datetime.strptime(started_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    deadline = dt.datetime.strptime(deadline_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
except ValueError as exc:
    raise SystemExit(f"external-review deadline record is malformed: {exc}")
if deadline <= started or deadline > started + dt.timedelta(hours=2):
    raise SystemExit("external-review deadline record exceeds two-hour bound")
record = {
    "version": 1,
    "root_id": root_id,
    "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
}
try:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit(3)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
finally:
    # fdopen closes on normal and exceptional exits after assignment.
    pass
try:
    directory = os.open(os.path.dirname(path), os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except OSError as exc:
    raise SystemExit(f"cannot sync external-review deadline record: {exc}")
PY
}

delivery_refresh_root_metadata() {
  DELIVERY_GC_TIMEOUT="$1"
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
remaining = int((deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()) - 1
if remaining <= 0:
    raise SystemExit("external-review deadline has expired")
print(f"{remaining}s")
PY
}

delivery_deadline_io_timeout() {
  local remaining
  remaining="$(delivery_remaining_deadline_timeout)" || return 1
  if [ "${remaining%s}" -lt "${DEADLINE_IO_TIMEOUT%s}" ]; then
    printf '%s' "$remaining"
  else
    printf '%s' "$DEADLINE_IO_TIMEOUT"
  fi
}

delivery_persist_recorded_pair() {
  local timeout_value
  timeout_value="$(delivery_deadline_io_timeout)" || return 1
  timeout --signal=KILL "$timeout_value" gc bd update "$DELIVERY_ROOT_ID" \
    --set-metadata "delivery.external_review_started_at=$1" \
    --set-metadata "delivery.external_review_deadline=$2"
}

delivery_load_record_pair() {
  local output
  output="$(delivery_read_deadline_record)" || return 1
  readarray -t RECORD_PAIR <<<"$output"
  [ "${#RECORD_PAIR[@]}" -eq 2 ] || return 1
  RECORD_STARTED_AT="${RECORD_PAIR[0]}"
  RECORD_DEADLINE="${RECORD_PAIR[1]}"
}

if { [ -n "$STARTED_AT" ] && [ -z "$DEADLINE" ]; } || { [ -z "$STARTED_AT" ] && [ -n "$DEADLINE" ]; }; then
  delivery_fail "external-review deadline metadata is partially missing"
fi

if [ "$MODE" = "--initialize" ] && [ -n "$STARTED_AT" ] && [ ! -e "$DEADLINE_RECORD" ]; then
  # A repair can be installed between the legacy metadata write and a later
  # validation.  Seed its first immutable record from that already-durable
  # pair only through the explicit initializer, never validate.
  delivery_validate_deadline_pair "$STARTED_AT" "$DEADLINE" || \
    delivery_fail "external-review deadline is missing, malformed, moved, or expired"
  delivery_remaining_deadline_timeout >/dev/null || delivery_fail "external-review deadline has expired"
  if delivery_create_deadline_record "$STARTED_AT" "$DEADLINE"; then
    :
  else
    create_status=$?
    [ "$create_status" -eq 3 ] || delivery_fail "cannot create external-review deadline record"
  fi
fi

if [ "$MODE" = "--initialize" ] && [ -z "$STARTED_AT" ]; then
  # O_EXCL is the first-write serialization point.  A waiter never replaces
  # the record; it reuses its exact pair and can finish a creator's interrupted
  # metadata update after bounded re-reads.
  record_creator=false
  if [ ! -e "$DEADLINE_RECORD" ]; then
    NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    INITIAL_DEADLINE="$(python3 - "$NOW" <<'PY'
import datetime as dt
import sys
started = dt.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
print((started + dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
    )"
    if delivery_create_deadline_record "$NOW" "$INITIAL_DEADLINE"; then
      record_creator=true
    else
      create_status=$?
      [ "$create_status" -eq 3 ] || delivery_fail "cannot create external-review deadline record"
    fi
  fi
  delivery_load_record_pair || delivery_fail "cannot read external-review deadline record"
  delivery_refresh_root_metadata "$DEADLINE_IO_TIMEOUT"
  if { [ -n "$STARTED_AT" ] && [ -z "$DEADLINE" ]; } || { [ -z "$STARTED_AT" ] && [ -n "$DEADLINE" ]; }; then
    delivery_fail "external-review deadline metadata is partially missing"
  fi
  if [ -z "$STARTED_AT" ]; then
    # A second initializer waits for the creator's bounded update.  If that
    # process crashed, it safely completes the exact O_EXCL-recorded pair.
    if [ "$record_creator" = false ]; then
      for _ in 1 2 3 4 5; do
        sleep 1
        delivery_refresh_root_metadata "$DEADLINE_IO_TIMEOUT"
        [ -n "$STARTED_AT" ] && break
      done
    fi
    if [ -z "$STARTED_AT" ]; then
      STARTED_AT="$RECORD_STARTED_AT"
      DEADLINE="$RECORD_DEADLINE"
      delivery_remaining_deadline_timeout >/dev/null || delivery_fail "external-review deadline expired before persistence"
      delivery_persist_recorded_pair "$STARTED_AT" "$DEADLINE" || \
        delivery_fail "cannot persist external-review deadline on workflow root before expiration"
      delivery_refresh_root_metadata "$DEADLINE_IO_TIMEOUT"
    fi
  fi
fi

delivery_load_record_pair || delivery_fail "external-review deadline record is missing or invalid"
delivery_validate_deadline_pair "$STARTED_AT" "$DEADLINE" || \
  delivery_fail "external-review deadline is missing, malformed, moved, or expired"
if [ "$STARTED_AT" != "$RECORD_STARTED_AT" ] || [ "$DEADLINE" != "$RECORD_DEADLINE" ]; then
  delivery_fail "external-review deadline current value does not match immutable record"
fi
delivery_remaining_deadline_timeout >/dev/null || delivery_fail "external-review deadline has expired"

echo "complete-delivery external-review deadline valid: $DEADLINE"
