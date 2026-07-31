#!/bin/sh
set -eu

if [ -z "${GC_PACK_DIR:-}" ]; then
  echo "gc complete-delivery report publish: missing Gas City pack context" >&2
  exit 1
fi
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] || [ -z "${1:-}" ]; then
  cat "$GC_PACK_DIR/commands/report/publish/help.md"
  [ -z "${1:-}" ] && exit 2 || exit 0
fi
command -v python3 >/dev/null 2>&1 || {
  echo "gc complete-delivery report publish: python3 is not on PATH" >&2
  exit 1
}

exec python3 "$GC_PACK_DIR/assets/scripts/publish_delivery_report.py" "$@"
