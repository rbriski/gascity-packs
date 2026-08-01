#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

cd "$DELIVERY_WORK_DIR"

ALLOW_NONE="$(delivery_var allow_no_local_gates false)"
RAN=0

run_gate() {
  local label="$1"
  local key="$2"
  local command
  command="$(delivery_var "$key" "")"
  [ -n "$command" ] || return 0
  RAN=$((RAN + 1))
  echo "complete-delivery local gate [$label]: $command"
  if ! bash -lc "$command"; then
    delivery_fail "$label command failed: $command"
  fi
}

run_gate setup setup_command
run_gate lint lint_command
run_gate typecheck typecheck_command
run_gate test test_command
run_gate build build_command
run_gate browser browser_test_command
run_gate security security_command
run_gate extra extra_gate_command

if [ "$RAN" -eq 0 ] && [ "$ALLOW_NONE" != "true" ]; then
  delivery_fail "no local quality commands are configured; set rig formula_vars or explicitly set allow_no_local_gates=true"
fi

echo "complete-delivery local gates passed ($RAN command(s))"
