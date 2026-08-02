#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

cd "$DELIVERY_WORK_DIR"

ALLOW_NONE="$(delivery_var allow_no_local_gates false)"
RAN=0

parse_local_gate_argv() {
  local command="$1"
  local output
  output="$(mktemp "${TMPDIR:-/tmp}/delivery-local-gate.XXXXXX")" || \
    delivery_fail "cannot allocate local-gate parser output"

  # Configuration is policy, never shell source. The embedded parser accepts
  # exactly one executable and literal argv values; it deliberately implements
  # only ordinary quoting and escaping, not any shell language feature.
  if ! python3 - "$command" >"$output" <<'PY'
import os
import pathlib
import sys

source = sys.argv[1]
args = []
word = []
state = None
started = False
i = 0

def fail(message: str) -> None:
    raise SystemExit(
        "local gate command is unsupported; terminal remote approval gate must not execute: "
        + message
    )

def append_session_default() -> None:
    word.append(os.environ.get("GC_SESSION_ID") or "manual")

def finish_word() -> None:
    global started
    if started:
        args.append("".join(word))
        word.clear()
        started = False

while i < len(source):
    character = source[i]
    if state is None:
        if character.isspace():
            finish_word()
        elif character in ";&|()<>":
            fail(f"control or redirection operator {character!r}")
        elif character in "{}":
            fail(f"brace expansion or grouping {character!r}")
        elif character in "*?[]":
            fail(f"glob expansion {character!r}")
        elif character == "`":
            fail("command substitution")
        elif character in "'\"":
            state = character
            started = True
        elif character == "\\":
            i += 1
            if i == len(source) or source[i] in "\r\n":
                fail("backslash line continuation")
            word.append(source[i])
            started = True
        elif character == "$" and source.startswith("$(", i):
            fail("command substitution")
        elif character == "$":
            if source.startswith("${GC_SESSION_ID:-manual}", i):
                if not args:
                    fail("session-default interpolation in executable")
                append_session_default()
                started = True
                i += len("${GC_SESSION_ID:-manual}") - 1
            else:
                fail("parameter, arithmetic, or command expansion")
        else:
            word.append(character)
            started = True
    elif state == "'":
        if character == "'":
            state = None
        else:
            # Shell does not expand dollars in single quotes; preserve them
            # verbatim so literal command arguments retain their meaning.
            word.append(character)
    else:
        if character == "\"":
            state = None
        elif character == "`":
            fail("command substitution")
        elif character == "\\":
            i += 1
            if i == len(source) or source[i] in "\r\n":
                fail("backslash line continuation")
            # In double quotes, Bash only consumes a backslash before special
            # escaped characters; preserve it for every other literal character.
            if source[i] not in '$"\\' and source[i] != chr(96):
                word.append("\\")
            word.append(source[i])
        elif character == "$" and source.startswith("$(", i):
            fail("command substitution")
        elif character == "$":
            if source.startswith("${GC_SESSION_ID:-manual}", i):
                if not args:
                    fail("session-default interpolation in executable")
                append_session_default()
                i += len("${GC_SESSION_ID:-manual}") - 1
            else:
                fail("parameter, arithmetic, or command expansion")
        else:
            word.append(character)
    i += 1

if state is not None:
    fail("unterminated quote")
finish_word()
if not args:
    fail("empty command")

executable = pathlib.PurePath(args[0]).name.lower()
blocked = {
    ".", "bash", "command", "dash", "delivery-pr-approved.sh",
    "delivery_gate.py", "env", "eval", "exec", "fish", "gh", "ksh",
    "sh", "source", "zsh", "coderabbit",
}
wrappers = {
    "chronic", "chrt", "ionice", "nice", "nohup", "prlimit", "setsid",
    "stdbuf", "taskset", "time", "timeout", "unshare", "xargs",
}
# These launchers can change credentials or reinterpret a literal nested command
# string (for example, ``su -c 'gh pr checks'``).  The restricted argv parser
# intentionally does not parse a second shell language inside an argument, so
# reject the bounded privilege/user-switch category before executing anything.
privilege_user_switch_wrappers = {
    "doas", "pkexec", "runuser", "setpriv", "su", "sudo",
}
provider_or_terminal = {"coderabbit", "delivery-pr-approved.sh", "delivery_gate.py", "gh"}
argument_names = [pathlib.PurePath(argument).name.lower() for argument in args]

def inline_program_present(executable: str, options: list[str]) -> bool:
    """Recognize each supported interpreter's program-bearing argv forms."""
    if executable in {"node", "nodejs"}:
        flags = ("-e", "-p", "--eval", "--print")
        option_operands = ("-r", "--require", "--import")
    elif executable == "python" or executable.startswith("python3"):
        flags = ("-c",)
        option_operands = ("-X", "-W")
    elif executable.startswith("perl"):
        flags = ("-e", "-E")
        option_operands = ("-I", "-M", "-m", "-F", "-x")
    elif executable.startswith("ruby"):
        flags = ("-e",)
        option_operands = ("-I", "-r", "-C", "-E", "-F", "-T", "-W")
    elif executable in {"awk", "gawk", "mawk", "nawk"}:
        flags = ("-e", "--execute", "--source")
        option_operands = ("-v", "--assign", "-i", "--include", "-l", "--load")
    else:
        return False

    awk = executable in {"awk", "gawk", "mawk", "nawk"}
    short_inline_flags = {flag[1:] for flag in flags if len(flag) == 2}
    short_operand_flags = {
        flag[1:] for flag in option_operands if len(flag) == 2
    }

    def clustered_program(argument: str) -> bool:
        if awk or not argument.startswith("-") or argument.startswith("--"):
            return False
        for flag in argument[1:]:
            if flag in short_inline_flags:
                return True
            if flag in short_operand_flags:
                return False
        return False

    selected = index = 0
    while index < len(options):
        argument = options[index]
        if selected and not awk:
            return False
        if argument == "--":
            return awk and not selected
        if clustered_program(argument):
            return True
        if any(
            argument == flag
            or argument.startswith(flag + "=")
            or (flag.startswith("-") and not flag.startswith("--") and argument.startswith(flag))
            for flag in flags
        ):
            return True
        if awk and (
            argument in {"-f", "--file", "-E", "--exec"}
            or argument.startswith(("-f", "--file=", "-E", "--exec="))
        ):
            selected = True
            if argument in {"-f", "--file", "-E", "--exec"}:
                index += 1
        elif argument in option_operands:
            index += 1
        elif awk and not selected and not argument.startswith("-"):
            # awk's first positional argument is its program unless -f/-E selected a file.
            return True
        elif executable == "python" or executable.startswith("python3"):
            if argument == "-m" or argument.startswith("-m"):
                selected = True
                if argument == "-m":
                    index += 1
            elif not argument.startswith("-"):
                selected = True
        elif not argument.startswith("-"):
            # Node, Perl, and Ruby select a script at their first positional arg.
            selected = True
        index += 1
    return False

if (
    executable in blocked
    or executable in wrappers
    or executable in privilege_user_switch_wrappers
    or any(name in provider_or_terminal for name in argument_names)
    or executable.startswith(("remote-approval", "remote_approval", "approval-gate", "approval_gate"))
    or any(name.startswith(("remote-approval", "remote_approval", "approval-gate", "approval_gate")) for name in argument_names)
    or any("api.github.com" in argument.lower() for argument in args)
    # The configured argv is trusted policy, but an inline program is a second
    # language boundary whose content cannot be meaningfully basename-scanned.
    or inline_program_present(executable, args[1:])
):
    raise SystemExit(
        "local gate command invokes a terminal remote approval gate or provider command"
    )

sys.stdout.buffer.write(b"\0".join(argument.encode() for argument in args) + b"\0")
PY
  then
    rm -f "$output"
    delivery_fail "local gate command could not be parsed safely: $command"
  fi

  # Bash 3.2 supports read -d, unlike mapfile -d; retain NUL-safe argv
  # transfer without imposing a newer Bash floor (notably on macOS).
  LOCAL_GATE_ARGV=()
  local argument
  while IFS= read -r -d '' argument; do
    LOCAL_GATE_ARGV+=("$argument")
  done <"$output"
  rm -f "$output"
  # Bash 3.2 and 4.2 can regard an assigned empty array as unset under
  # nounset. Check its declaration before expanding its length so malformed
  # parser output reaches the pack's fail-closed diagnostic.
  if [ "${LOCAL_GATE_ARGV+x}" != x ] || [ "${#LOCAL_GATE_ARGV[@]}" -eq 0 ]; then
    delivery_fail "local gate command could not be parsed safely: $command"
  fi
}

run_gate() {
  local label="$1"
  local key="$2"
  local command
  command="$(delivery_var "$key" "")"
  [ -n "$command" ] || return 0
  parse_local_gate_argv "$command"
  RAN=$((RAN + 1))
  echo "complete-delivery local gate [$label]: $command"
  if ! "${LOCAL_GATE_ARGV[@]}"; then
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
