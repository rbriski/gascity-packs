#!/usr/bin/env python3
"""Company-rooms outbound surface for the slack-full pack (company rooms 2b).

Python owns company *outbound* (the Discord ownership split): posting
intents, per-agent token files, ``chat.postMessage``, delegation-record
creation, receipt-based lazy recovery, and pruning. Go owns ingress
(author resolution, trust, result-claim/expiry transitions, the
current-turn pointer). Both sides share on-disk state under the adapter
state root, serialized by the lock contract and validated fail-closed on
both sides.

This module is a library: the ``gc slack delegate`` verb
(``cmd_delegate``/``cmd_cancel``) and ``reply-current``'s company-context
path both call into it. Every filename/lock/nonce derivation here is a
byte-for-byte match of ``adapter/company_sanitize.go`` — the golden
fixtures in ``tests/fixtures/company/`` pin the two sides together.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

DEFAULT_SLACK_API_BASE = "https://slack.com/api"

# Intent lifetime and retry policy defaults (shared cross-language contract).
INTENT_TTL_SECONDS = 86400
DEFAULT_MAX_ATTEMPTS = 3
RETRY_DEADLINE_SECONDS = 120

# Terminal/expired records and terminal intents are pruned after this many
# seconds. Pinned at 7 days (receipt/Discord parity); a caller-supplied
# retention below the 24h floor is clamped up, never honored — terminal state
# is the crash-recovery memory the reconciler leans on.
PRUNE_RETENTION_SECONDS = 7 * 86400
PRUNE_RETENTION_FLOOR_SECONDS = 24 * 3600

# Slack app-level errors (HTTP 200 with ok:false) that are worth retrying —
# everything else at ok:false is a definitive rejection (bad channel, bad
# auth, not-in-channel, ...) that must fail the intent, never repost.
_TRANSIENT_SLACK_ERRORS = frozenset({
    "ratelimited",
    "internal_error",
    "service_unavailable",
    "fatal_error",
    "request_timeout",
    "timeout",
})

# Seam so tests can drive the retry loop without real sleeping.
_sleep = time.sleep


class OutboundError(RuntimeError):
    """Raised on an invalid input or an unrecoverable outbound failure."""


class DefinitivePostError(RuntimeError):
    """A provider rejection that must mark the intent ``failed`` (no repost)."""


class TransientPostError(RuntimeError):
    """A timeout/5xx/rate-limit that must reconcile before any repost."""


# --- path resolution ------------------------------------------------------
#
# Precedence mirrors Phase 1 exactly: explicit env override >
# <GC_CITY_PATH>/.gc/slack/<leaf> > /tmp/gc-slack-adapter/<leaf>.

def _state_dir(env_override: str, leaf: str) -> pathlib.Path:
    override = os.environ.get(env_override, "").strip()
    if override:
        return pathlib.Path(override)
    city = os.environ.get("GC_CITY_PATH", "").strip()
    if city:
        return pathlib.Path(city) / ".gc" / "slack" / leaf
    return pathlib.Path("/tmp/gc-slack-adapter") / leaf


def secrets_dir() -> pathlib.Path:
    return _state_dir("SLACK_COMPANY_SECRETS_DIR", "secrets")


def intents_dir() -> pathlib.Path:
    return _state_dir("SLACK_COMPANY_INTENTS_DIR", "company-delegation-intents")


def delegations_dir() -> pathlib.Path:
    return _state_dir("SLACK_COMPANY_DELEGATIONS_DIR", "company-delegations")


def turns_dir() -> pathlib.Path:
    return _state_dir("SLACK_COMPANY_TURNS_DIR", "company-current-turn")


def locks_dir() -> pathlib.Path:
    return _state_dir("SLACK_COMPANY_LOCKS_DIR", "locks")


def ingress_dir() -> pathlib.Path:
    return _state_dir("SLACK_COMPANY_INGRESS_DIR", "chat-ingress")


# --- filename / lock / nonce sanitizer (byte-for-byte cross-language) ------

def component_safe(component: str) -> bool:
    """Whether a filename component passes the sanitizer unchanged.

    A component is hashed when it contains a byte outside ``[A-Za-z0-9._-]``,
    begins with ``.``, equals ``..``, or exceeds 64 bytes. Length and the
    byte alphabet are checked on the UTF-8 encoding so this matches Go's
    ``companyComponentSafe`` byte for byte.
    """
    raw = component.encode("utf-8")
    if len(raw) > 64 or component == ".." or component.startswith("."):
        return False
    for ch in raw:
        if (
            0x41 <= ch <= 0x5A  # A-Z
            or 0x61 <= ch <= 0x7A  # a-z
            or 0x30 <= ch <= 0x39  # 0-9
            or ch in (0x2E, 0x5F, 0x2D)  # . _ -
        ):
            continue
        return False
    return True


def sanitize_component(component: str) -> str:
    """``component`` when filename-safe, else ``h`` + sha256hex(component)[:16]."""
    if component_safe(component):
        return component
    digest = hashlib.sha256(component.encode("utf-8")).hexdigest()
    return "h" + digest[:16]


def tuple_digest12(team_id: str, channel_id: str, ts: str) -> str:
    """12-hex disambiguating suffix over the raw NUL-joined origin."""
    joined = f"{team_id}\x00{channel_id}\x00{ts}".encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:12]


def delegation_filename(team_id: str, channel_id: str, ts: str) -> str:
    """Delegations-registry filename keyed by (team, channel, posted ts)."""
    return (
        "dg-"
        + sanitize_component(team_id) + "-"
        + sanitize_component(channel_id) + "-"
        + sanitize_component(ts) + "-"
        + tuple_digest12(team_id, channel_id, ts) + ".json"
    )


def lock_filename(label: str, *key_fields: str) -> str:
    """``<label>-<sha256hex(NUL-joined key fields)[:16]>.lock``."""
    joined = "\x00".join(key_fields).encode("utf-8")
    return label + "-" + hashlib.sha256(joined).hexdigest()[:16] + ".lock"


def dtuple_lock_name(
    team_id: str,
    channel_id: str,
    thread_root_ts: str,
    responder_bot_user_id: str,
    requester_bot_user_id: str,
) -> str:
    return lock_filename(
        "dtuple", team_id, channel_id, thread_root_ts,
        responder_bot_user_id, requester_bot_user_id,
    )


def intent_lock_name(nonce: str) -> str:
    return lock_filename("intent", nonce)


def compute_nonce(
    *,
    source_app_id: str,
    source_bot_user_id: str,
    target_agent: str,
    target_bot_user_id: str,
    team_id: str,
    channel_id: str,
    human_root_ts: str,
    body_sha256: str,
    retry_seq: int,
) -> str:
    """``gcs-`` + first 20 hex of sha256 over the canonical anticipated record.

    The nine fields are NUL-joined (``retry_seq`` as its decimal string), so a
    fresh body or a fresh ``retry_seq`` mints a distinct nonce while a crash
    retry of the same in-flight delegation reproduces it exactly.
    """
    fields = (
        source_app_id,
        source_bot_user_id,
        target_agent,
        target_bot_user_id,
        team_id,
        channel_id,
        human_root_ts,
        body_sha256,
        str(int(retry_seq)),
    )
    digest = hashlib.sha256("\x00".join(fields).encode("utf-8")).hexdigest()
    return "gcs-" + digest[:20]


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def derive_human_root_ts(thread_ts: str, ts: str) -> str:
    """Normative root derivation: ``thread_ts`` when non-empty, else ``ts``.

    ``thread_ts`` on ``chat.postMessage`` must always be a parent — Slack
    documents replying to a reply as invalid — so a threaded trigger derives
    the parent root and an unthreaded trigger roots at its own ts.
    """
    thread_ts = (thread_ts or "").strip()
    return thread_ts if thread_ts else (ts or "").strip()


# --- time helpers ---------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_rfc3339(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- advisory lock --------------------------------------------------------

class _FlockGuard:
    """A held ``flock(LOCK_EX)``; released on context exit, nil-safe."""

    def __init__(self, fd: int, path: pathlib.Path) -> None:
        self._fd = fd
        self._path = path

    def __enter__(self) -> "_FlockGuard":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()

    def release(self) -> None:
        if self._fd < 0:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = -1


def acquire_lock(name: str) -> _FlockGuard:
    """Take an exclusive advisory lock on ``locks/<name>`` (blocking)."""
    ldir = locks_dir()
    ldir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = ldir / name
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return _FlockGuard(fd, path)


# --- token loader ---------------------------------------------------------

def load_bot_token(agent: str) -> str:
    """Read ``secrets/bot-token-<agent>.txt`` with permission/symlink refusal.

    The directory must be 0700, the token file 0600, and neither may be a
    symlink. These are validation (not trust) checks: a token file with lax
    permissions is refused so a same-UID squatter cannot substitute one.
    """
    if not agent:
        raise OutboundError("token load requires an agent name")
    sdir = secrets_dir()
    try:
        dinfo = os.lstat(sdir)
    except OSError as exc:
        raise OutboundError(f"secrets dir {sdir} is unavailable: {exc}") from exc
    if stat.S_ISLNK(dinfo.st_mode):
        raise OutboundError(f"secrets dir {sdir} is a symlink; refusing")
    if not stat.S_ISDIR(dinfo.st_mode):
        raise OutboundError(f"secrets dir {sdir} is not a directory")
    if stat.S_IMODE(dinfo.st_mode) != 0o700:
        raise OutboundError(
            f"secrets dir {sdir} must be mode 0700, got {stat.S_IMODE(dinfo.st_mode):04o}")

    path = sdir / f"bot-token-{agent}.txt"
    try:
        finfo = os.lstat(path)
    except OSError as exc:
        raise OutboundError(f"no bot token for agent {agent!r} at {path}: {exc}") from exc
    if stat.S_ISLNK(finfo.st_mode):
        raise OutboundError(f"token file {path} is a symlink; refusing")
    if not stat.S_ISREG(finfo.st_mode):
        raise OutboundError(f"token file {path} is not a regular file")
    if stat.S_IMODE(finfo.st_mode) != 0o600:
        raise OutboundError(
            f"token file {path} must be mode 0600, got {stat.S_IMODE(finfo.st_mode):04o}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        raise OutboundError(f"cannot read token file {path}: {exc}") from exc
    if not token:
        raise OutboundError(f"token file {path} is empty")
    return token


# --- atomic / create-once writers -----------------------------------------

def _dumps(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write_json(path: pathlib.Path, obj: dict[str, Any]) -> None:
    """Atomic tmp+fsync+rename, 0600 file in a 0700 dir; symlinked dest refused."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        dest = os.lstat(path)
    except FileNotFoundError:
        dest = None
    if dest is not None and stat.S_ISLNK(dest.st_mode):
        raise OutboundError(f"refusing to write {path}: destination is a symlink")
    data = _dumps(obj)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = pathlib.Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _create_once_json(path: pathlib.Path, obj: dict[str, Any]) -> bool:
    """Create ``path`` exactly once (``O_EXCL``). Returns False on ``EEXIST``.

    An existing file is never overwritten — the caller adopts it. The write is
    fsynced before the fd closes so a crash leaves either nothing or the full
    record.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(_dumps(obj))
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return True


def _read_json(path: pathlib.Path) -> dict[str, Any] | None:
    """Read+parse a JSON object, refusing symlinks. None if absent/corrupt."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


# --- record parsers (fail-closed; validate the golden fixtures) -----------

_INTENT_STATUSES = frozenset({"prepared", "posting", "published", "failed", "expired"})
_INTENT_TERMINAL = frozenset({"published", "failed", "expired"})
# An intent's ``op`` labels which outbound post it durably backs. The receipt
# scan gates on the op's Slack metadata ``event_type`` so a delegation echo can
# never be mistaken for a result echo carrying the same (delegation) nonce.
_INTENT_OPS = frozenset({"delegation", "result", "synthesis"})
_OP_EVENT_TYPES = {
    "delegation": "gc_delegation",
    "result": "gc_delegation_result",
    "synthesis": "gc_delegation_synthesis",
}
_DELEGATION_STATUSES = frozenset({"pending", "result_claimed", "expired"})
_DELEGATION_TERMINAL = frozenset({"result_claimed", "expired"})
_TURN_KINDS = frozenset({
    "ambient", "targeted", "peer_delegation", "peer_result", "peer_input",
})

_INTENT_STR_FIELDS = (
    "nonce", "status", "created_at", "updated_at", "retry_deadline",
    "source_agent", "source_app_id", "source_bot_user_id",
    "target_agent", "target_bot_user_id", "team_id", "channel_id", "room",
    "human_root_ts", "requester_session", "body_sha256",
)
_DELEGATION_STR_FIELDS = (
    "nonce", "room", "team_id", "channel_id", "ts", "thread_root_ts",
    "requester_agent", "requester_bot_user_id", "requester_session",
    "expected_responder_agent", "expected_responder_bot_user_id",
    "created_at", "status",
)
_TURN_STR_FIELDS = (
    "session", "receipt_id", "team_id", "channel_id", "ts", "room", "kind",
    "thread_root_ts", "agent", "delivered_at",
)


def _require(data: dict[str, Any], key: str, ctx: str) -> Any:
    if key not in data:
        raise OutboundError(f"{ctx} missing required field {key!r}")
    return data[key]


def _require_str(data: dict[str, Any], key: str, ctx: str, *, allow_empty: bool = False) -> str:
    val = _require(data, key, ctx)
    if not isinstance(val, str) or (not allow_empty and not val):
        raise OutboundError(f"{ctx} field {key!r} must be a non-empty string, got {val!r}")
    return val


def parse_intent(data: dict[str, Any]) -> dict[str, Any]:
    ctx = "intent"
    if not isinstance(data, dict):
        raise OutboundError("intent must be an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise OutboundError(f"intent schema_version must be {SCHEMA_VERSION}")
    for key in _INTENT_STR_FIELDS:
        allow_empty = key in ("created_at", "updated_at", "retry_deadline")
        _require_str(data, key, ctx, allow_empty=allow_empty)
    if data["status"] not in _INTENT_STATUSES:
        raise OutboundError(f"intent status invalid: {data['status']!r}")
    if not data["nonce"].startswith("gcs-"):
        raise OutboundError(f"intent nonce must start with 'gcs-', got {data['nonce']!r}")
    # ``op``/``metadata_nonce`` are additive (default to a delegation post whose
    # metadata carries its own nonce) so a pre-op intent parses unchanged.
    op = data.setdefault("op", "delegation")
    if op not in _INTENT_OPS:
        raise OutboundError(f"intent op invalid: {op!r}")
    mnonce = data.setdefault("metadata_nonce", data["nonce"])
    if not isinstance(mnonce, str) or not mnonce:
        raise OutboundError("intent metadata_nonce must be a non-empty string")
    for key in ("retry_seq", "attempts", "max_attempts", "ttl_seconds"):
        val = data.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise OutboundError(f"intent field {key!r} must be a non-negative int, got {val!r}")
    # posted_ts is present but may be empty until published.
    if not isinstance(data.get("posted_ts", ""), str):
        raise OutboundError("intent posted_ts must be a string")
    return data


def parse_delegation(data: dict[str, Any]) -> dict[str, Any]:
    ctx = "delegation"
    if not isinstance(data, dict):
        raise OutboundError("delegation must be an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise OutboundError(f"delegation schema_version must be {SCHEMA_VERSION}")
    gen = data.get("generation")
    if isinstance(gen, bool) or not isinstance(gen, int) or gen < 1:
        raise OutboundError(f"delegation generation must be a positive int, got {gen!r}")
    for key in _DELEGATION_STR_FIELDS:
        _require_str(data, key, ctx)
    if data["status"] not in _DELEGATION_STATUSES:
        raise OutboundError(f"delegation status invalid: {data['status']!r}")
    ttl = data.get("ttl_seconds")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        raise OutboundError(f"delegation ttl_seconds must be a non-negative int, got {ttl!r}")
    for key in ("result_ts", "result_claimed_at"):
        if not isinstance(data.get(key, ""), str):
            raise OutboundError(f"delegation {key!r} must be a string")
    return data


def parse_current_turn(data: dict[str, Any]) -> dict[str, Any]:
    ctx = "current_turn"
    if not isinstance(data, dict):
        raise OutboundError("current_turn must be an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise OutboundError(f"current_turn schema_version must be {SCHEMA_VERSION}")
    for key in _TURN_STR_FIELDS:
        _require_str(data, key, ctx)
    if data["kind"] not in _TURN_KINDS:
        raise OutboundError(f"current_turn kind invalid: {data['kind']!r}")
    # A delegation_key is required only for the correlated peer kinds
    # (peer_delegation/peer_result). Uncorrelated peer wakes (peer_input) and
    # room-context turns (ambient/targeted) carry no key — Go emits them
    # keyless, and the reply path posts into the thread root without a record.
    if data["kind"] in ("peer_delegation", "peer_result"):
        _require_str(data, "delegation_key", ctx)
    return data


# --- intent store (create-once + attempts-CAS under the intent lock) -------

def _intent_path(nonce: str) -> pathlib.Path:
    return intents_dir() / f"{nonce}.json"


def list_intents() -> list[dict[str, Any]]:
    """Every parseable intent record (corrupt files skipped)."""
    out: list[dict[str, Any]] = []
    idir = intents_dir()
    try:
        names = sorted(os.listdir(idir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        data = _read_json(idir / name)
        if data is None:
            continue
        try:
            out.append(parse_intent(data))
        except OutboundError:
            continue
    return out


def _tuple8(data: dict[str, Any]) -> tuple[str, ...]:
    return (
        data["source_app_id"], data["source_bot_user_id"],
        data["target_agent"], data["target_bot_user_id"],
        data["team_id"], data["channel_id"],
        data["human_root_ts"], data["body_sha256"],
    )


def next_retry_seq(tuple8: tuple[str, ...]) -> int:
    """One past the highest ``retry_seq`` seen for the tuple (0 when none).

    A monotonic max+1 (not a surviving-file count) so pruning terminal intents
    can never shrink the sequence and re-mint a nonce a stale receipt could
    still reconcile against — the pruner always retains the highest-seq intent
    per tuple as the watermark.
    """
    seqs = [int(i.get("retry_seq", 0)) for i in list_intents() if _tuple8(i) == tuple8]
    return (max(seqs) + 1) if seqs else 0


def find_nonterminal_intent_for_tuple(tuple8: tuple[str, ...]) -> dict[str, Any] | None:
    for intent in list_intents():
        if _tuple8(intent) == tuple8 and intent["status"] not in _INTENT_TERMINAL:
            return intent
    return None


def reusable_intent_for_tuple(tuple8: tuple[str, ...]) -> dict[str, Any] | None:
    """The intent a re-run should reuse for this tuple, or None.

    Prefers a ``published`` intent (the post already landed → idempotent
    success, never repost) over a non-terminal one (``posting`` reconciles;
    ``prepared`` resumes). A failed/expired intent is ignored so a fresh
    higher-seq retry can proceed.
    """
    published: dict[str, Any] | None = None
    nonterminal: dict[str, Any] | None = None
    for intent in list_intents():
        if _tuple8(intent) != tuple8:
            continue
        status = intent["status"]
        if status == "published":
            published = intent
        elif status not in _INTENT_TERMINAL:
            nonterminal = intent
    return published or nonterminal


def build_intent(
    *,
    nonce: str,
    retry_seq: int,
    source_agent: str,
    source_app_id: str,
    source_bot_user_id: str,
    target_agent: str,
    target_bot_user_id: str,
    team_id: str,
    channel_id: str,
    room: str,
    human_root_ts: str,
    requester_session: str,
    body_hex: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    op: str = "delegation",
    metadata_nonce: str = "",
) -> dict[str, Any]:
    now = _rfc3339(_now())
    return {
        "schema_version": SCHEMA_VERSION,
        "nonce": nonce,
        "op": op,
        "metadata_nonce": metadata_nonce or nonce,
        "retry_seq": int(retry_seq),
        "status": "prepared",
        "attempts": 0,
        "max_attempts": int(max_attempts),
        "created_at": now,
        "updated_at": now,
        "retry_deadline": "",
        "ttl_seconds": INTENT_TTL_SECONDS,
        "source_agent": source_agent,
        "source_app_id": source_app_id,
        "source_bot_user_id": source_bot_user_id,
        "target_agent": target_agent,
        "target_bot_user_id": target_bot_user_id,
        "team_id": team_id,
        "channel_id": channel_id,
        "room": room,
        "human_root_ts": human_root_ts,
        "requester_session": requester_session,
        "body_sha256": body_hex,
        "posted_ts": "",
    }


def write_intent(intent: dict[str, Any]) -> None:
    _atomic_write_json(_intent_path(intent["nonce"]), intent)


def create_intent_once(intent: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Create the intent file exactly once. On ``EEXIST`` adopt the existing one."""
    path = _intent_path(intent["nonce"])
    if _create_once_json(path, intent):
        return intent, True
    existing = _read_json(path)
    if existing is None:
        raise OutboundError(f"intent {intent['nonce']} exists but is unreadable")
    return parse_intent(existing), False


def update_intent(nonce: str, mutate) -> dict[str, Any]:
    """Read-modify-write the intent under its lock (the attempts-CAS section)."""
    with acquire_lock(intent_lock_name(nonce)):
        data = _read_json(_intent_path(nonce))
        if data is None:
            raise OutboundError(f"intent {nonce} vanished under the lock")
        intent = parse_intent(data)
        mutate(intent)
        intent["updated_at"] = _rfc3339(_now())
        write_intent(intent)
        return intent


def intent_mark_posting(nonce: str) -> dict[str, Any]:
    def _m(intent: dict[str, Any]) -> None:
        intent["status"] = "posting"
        intent["attempts"] = int(intent.get("attempts", 0)) + 1
        if not intent.get("retry_deadline"):
            deadline = _now().timestamp() + RETRY_DEADLINE_SECONDS
            intent["retry_deadline"] = _rfc3339(datetime.fromtimestamp(deadline, timezone.utc))
    return update_intent(nonce, _m)


def intent_mark_published(nonce: str, posted_ts: str) -> dict[str, Any]:
    def _m(intent: dict[str, Any]) -> None:
        intent["status"] = "published"
        intent["posted_ts"] = posted_ts
    return update_intent(nonce, _m)


def intent_mark_failed(nonce: str, detail: str = "") -> dict[str, Any]:
    def _m(intent: dict[str, Any]) -> None:
        intent["status"] = "failed"
        if detail:
            intent["detail"] = detail
    return update_intent(nonce, _m)


# --- delegation records (create-once; O_EXCL, EEXIST adopts) ---------------

def _delegation_path(team_id: str, channel_id: str, ts: str) -> pathlib.Path:
    return delegations_dir() / delegation_filename(team_id, channel_id, ts)


def list_delegations() -> list[tuple[pathlib.Path, dict[str, Any]]]:
    out: list[tuple[pathlib.Path, dict[str, Any]]] = []
    ddir = delegations_dir()
    try:
        names = sorted(os.listdir(ddir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = ddir / name
        data = _read_json(path)
        if data is None:
            continue
        try:
            out.append((path, parse_delegation(data)))
        except OutboundError:
            continue
    return out


def build_delegation(intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generation": 1,
        "nonce": intent["nonce"],
        "room": intent["room"],
        "team_id": intent["team_id"],
        "channel_id": intent["channel_id"],
        "ts": intent["posted_ts"],
        "thread_root_ts": intent["human_root_ts"],
        "requester_agent": intent["source_agent"],
        "requester_bot_user_id": intent["source_bot_user_id"],
        "requester_session": intent["requester_session"],
        "expected_responder_agent": intent["target_agent"],
        "expected_responder_bot_user_id": intent["target_bot_user_id"],
        "created_at": _rfc3339(_now()),
        "ttl_seconds": int(intent.get("ttl_seconds", INTENT_TTL_SECONDS)),
        "status": "pending",
        "result_ts": "",
        "result_claimed_at": "",
    }


def materialize_delegation(intent: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Create the delegation record once, keyed by the posted (team, channel, ts).

    ``O_EXCL`` create; ``EEXIST`` adopts the existing record and never
    overwrites it (Go writes claim/expiry transitions to the same file).
    """
    if not intent.get("posted_ts"):
        raise OutboundError("cannot materialize a delegation before the post ts is known")
    record = build_delegation(intent)
    path = _delegation_path(intent["team_id"], intent["channel_id"], intent["posted_ts"])
    if _create_once_json(path, record):
        return record, True
    existing = _read_json(path)
    if existing is None:
        raise OutboundError(f"delegation {path.name} exists but is unreadable")
    return parse_delegation(existing), False


def _record_expired(record: dict[str, Any], now: datetime) -> bool:
    created = _parse_rfc3339(record.get("created_at", ""))
    if created is None:
        return False
    ttl = int(record.get("ttl_seconds", INTENT_TTL_SECONDS))
    return (now - created).total_seconds() > ttl


def expire_delegation(path: pathlib.Path, record: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a pending record to ``expired`` (generation bumped), in place."""
    record = dict(record)
    record["status"] = "expired"
    record["generation"] = int(record.get("generation", 1)) + 1
    _atomic_write_json(path, record)
    return record


def find_pending_delegation(
    team_id: str,
    channel_id: str,
    thread_root_ts: str,
    responder_bot_user_id: str,
    requester_bot_user_id: str,
    *,
    expire_stale: bool = True,
) -> tuple[pathlib.Path, dict[str, Any]] | None:
    """The one pending record for the tuple, or None.

    A TTL-expired ``pending`` record counts as not-pending: it is rewritten
    ``expired`` under the caller's tuple lock and skipped. The caller must
    already hold the ``dtuple`` lock.
    """
    now = _now()
    for path, record in list_delegations():
        if (
            record["team_id"] == team_id
            and record["channel_id"] == channel_id
            and record["thread_root_ts"] == thread_root_ts
            and record["expected_responder_bot_user_id"] == responder_bot_user_id
            and record["requester_bot_user_id"] == requester_bot_user_id
            and record["status"] == "pending"
        ):
            if _record_expired(record, now):
                if expire_stale:
                    expire_delegation(path, record)
                continue
            return path, record
    return None


# --- composition / escaping ------------------------------------------------

def escape_body(body: str) -> str:
    """Entity-escape ``&`` ``<`` ``>`` so agent text can never be a live entity."""
    return body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Slack member ids are ``U``/``W`` + uppercase alphanumerics. The body is
# entity-escaped so agent text can never be a live entity, but the service
# constructs ``<@id>`` around a directory/record-sourced id verbatim; a hostile
# id like ``U0> <!channel`` would break out into a live broadcast entity. Ids
# are validated to this charset before interpolation so the constructed mention
# stays the only live entity (composition contract).
_BOT_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")


def _validate_bot_user_id(bot_user_id: str, *, role: str) -> str:
    if not isinstance(bot_user_id, str) or not _BOT_USER_ID_RE.match(bot_user_id):
        raise OutboundError(
            f"{role} bot_user_id {bot_user_id!r} is not a valid Slack member id "
            "(^[UW][A-Z0-9]{2,}$); refusing to interpolate into a mention")
    return bot_user_id


def compose_delegation_text(target_bot_user_id: str, body: str) -> str:
    _validate_bot_user_id(target_bot_user_id, role="delegation target")
    return f"<@{target_bot_user_id}> {escape_body(body)}"


def compose_result_text(requester_bot_user_id: str, body: str) -> str:
    _validate_bot_user_id(requester_bot_user_id, role="result requester")
    return f"<@{requester_bot_user_id}> {escape_body(body)}"


def compose_synthesis_text(body: str) -> str:
    return escape_body(body)


def delegation_metadata(nonce: str, root_ts: str, requester: str, target: str) -> dict[str, Any]:
    return {
        "event_type": "gc_delegation",
        "event_payload": {"v": 1, "nonce": nonce, "root_ts": root_ts,
                          "requester": requester, "target": target},
    }


def result_metadata(nonce: str, delegation_ts: str) -> dict[str, Any]:
    return {
        "event_type": "gc_delegation_result",
        "event_payload": {"v": 1, "nonce": nonce, "delegation_ts": delegation_ts},
    }


def synthesis_metadata(nonce: str) -> dict[str, Any]:
    """Metadata for a synthesis post: carries only the reconcile nonce.

    A synthesis has no live mention (Go wakes nobody on it), so this event_type
    is inert to the router; it exists solely so a timed-out synthesis post can
    be reconciled against its own switchboard echo instead of reposted.
    """
    return {
        "event_type": "gc_delegation_synthesis",
        "event_payload": {"v": 1, "nonce": nonce},
    }


# --- provider POST (bounded retries, honor Retry-After) --------------------

def _slack_api_base() -> str:
    return os.environ.get("SLACK_API_BASE_URL", DEFAULT_SLACK_API_BASE).rstrip("/")


def _slack_web_post(
    method: str,
    token: str,
    payload: dict[str, Any],
    *,
    api_base: str,
    timeout: float,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """POST a JSON Slack Web API call. Returns (http_status, headers, body).

    Network errors and timeouts raise :class:`TransientPostError` — they are
    never a definitive rejection. This is the single seam tests monkeypatch.
    """
    url = f"{api_base}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            headers = {k: v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        headers = {k: v for k, v in (exc.headers or {}).items()}
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise TransientPostError(f"{method} transport failure: {exc}") from exc
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    return status, headers, body


def _retry_after_seconds(headers: dict[str, str], body: dict[str, Any]) -> float:
    raw = ""
    for key, val in headers.items():
        if key.lower() == "retry-after":
            raw = val
            break
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        secs = 1.0
    if secs < 0:
        secs = 1.0
    return min(secs, 30.0)


def post_message(
    token: str,
    *,
    channel: str,
    text: str,
    thread_ts: str = "",
    metadata: dict[str, Any] | None = None,
    api_base: str | None = None,
    timeout: float = 15.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str:
    """Post a company message and return the Slack ``ts``.

    Composition contract: top-level ``text`` only — no ``blocks``,
    ``link_names``, ``reply_broadcast``, or ``parse``. ``429`` honors
    ``Retry-After`` within ``max_attempts``; a definitive 4xx raises
    :class:`DefinitivePostError`; a timeout/5xx raises
    :class:`TransientPostError` (the caller reconciles before any repost).
    """
    base = api_base or _slack_api_base()
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if metadata is not None:
        payload["metadata"] = metadata

    for attempt in range(1, max_attempts + 1):
        status, headers, body = _slack_web_post(
            "chat.postMessage", token, payload, api_base=base, timeout=timeout)
        if status == 200 and body.get("ok"):
            ts = body.get("ts") or (body.get("message") or {}).get("ts")
            if not ts:
                raise DefinitivePostError("chat.postMessage ok but returned no ts")
            return str(ts)
        if status == 429:
            if attempt < max_attempts:
                _sleep(_retry_after_seconds(headers, body))
                continue
            raise TransientPostError("chat.postMessage rate-limited: attempts exhausted")
        if 500 <= status <= 599:
            raise TransientPostError(f"chat.postMessage http {status}")
        if status != 200:
            raise DefinitivePostError(f"chat.postMessage http {status}")
        err = str(body.get("error", "")) or "unknown_error"
        if err in _TRANSIENT_SLACK_ERRORS:
            if err in ("ratelimited", "rate_limited") and attempt < max_attempts:
                _sleep(_retry_after_seconds(headers, body))
                continue
            raise TransientPostError(f"chat.postMessage transient error: {err}")
        raise DefinitivePostError(f"chat.postMessage error: {err}")
    raise TransientPostError("chat.postMessage exhausted attempts")


# --- receipt-based reconciliation (read-only receipt scan) -----------------

def _iter_receipts() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rdir = ingress_dir()
    try:
        names = sorted(os.listdir(rdir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        data = _read_json(rdir / name)
        if isinstance(data, dict):
            out.append(data)
    return out


def _receipt_bot_authored(event: dict[str, Any]) -> bool:
    return bool(
        event.get("bot_id")
        or event.get("app_id")
        or event.get("bot_profile")
        or event.get("subtype") == "bot_message"
    )


def _receipt_author_matches(event: dict[str, Any], intent: dict[str, Any]) -> bool:
    """Whether a bot-authored receipt was posted by the intent's own identity.

    The nonce is workspace-visible (it rides in the message's own metadata), so
    a same-nonce message from any *other* bot in the room must never be adopted
    as this intent's post. Match the receipt's authoring app (``app_id``, incl.
    the ``bot_profile`` envelope) against ``source_app_id``, or the raw bot
    ``user`` against ``source_bot_user_id`` when no app id is present.
    """
    source_app = intent.get("source_app_id")
    app_id = event.get("app_id")
    if not app_id:
        bp = event.get("bot_profile")
        if isinstance(bp, dict):
            app_id = bp.get("app_id")
    if app_id:
        return bool(source_app) and app_id == source_app
    user = event.get("user") or ""
    return bool(user) and user == intent.get("source_bot_user_id")


def _scan_receipt_for_nonce(intent: dict[str, Any]) -> str | None:
    """Origin ts of the intent's own bot-authored receipt carrying its nonce.

    Read-only: the post, arriving back through the switchboard, is admitted as a
    bot-message receipt whose stored raw event embeds the posted metadata.
    Reconciliation adopts it only when the receipt is in (team, channel), was
    authored by the intent's own identity, carries the op's ``event_type``, and
    embeds the intent's metadata nonce. Reconciliation never calls the Slack API.
    """
    want_nonce = intent.get("metadata_nonce") or intent["nonce"]
    want_event_type = _OP_EVENT_TYPES.get(intent.get("op", "delegation"))
    for receipt in _iter_receipts():
        origin = receipt.get("origin") or {}
        if origin.get("team_id") != intent["team_id"]:
            continue
        if origin.get("channel_id") != intent["channel_id"]:
            continue
        event = receipt.get("event")
        if not isinstance(event, dict):
            continue
        if not _receipt_bot_authored(event):
            continue
        if not _receipt_author_matches(event, intent):
            continue
        metadata = event.get("metadata") or {}
        if want_event_type is not None and metadata.get("event_type") != want_event_type:
            continue
        payload = metadata.get("event_payload") or {}
        if payload.get("nonce") != want_nonce:
            continue
        ts = origin.get("ts")
        if ts:
            return str(ts)
    return None


def reconcile_intent(intent: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve one ``posting`` intent against the receipts.

    Found → adopt the origin ts as ``posted_ts``, mark ``published``, and
    materialize the delegation record if absent; returns the published intent.
    Not found → returns None (the intent stays parked; never reposted).
    """
    nonce = intent["nonce"]
    with acquire_lock(intent_lock_name(nonce)):
        data = _read_json(_intent_path(nonce))
        current = parse_intent(data) if data is not None else intent
        if current["status"] == "published":
            _ensure_record(current)
            return current
        if current["status"] != "posting":
            return None
        posted_ts = _scan_receipt_for_nonce(current)
        if posted_ts is None:
            return None
        current["status"] = "published"
        current["posted_ts"] = posted_ts
        current["updated_at"] = _rfc3339(_now())
        write_intent(current)
    _ensure_record(current)
    return current


def _ensure_record(intent: dict[str, Any]) -> None:
    # Only a delegation post owns a delegation record. Result/synthesis posts
    # are durable-but-recordless (Go owns the result-claim transition), so
    # reconciling them must never materialize a spurious pending record.
    if intent.get("op", "delegation") == "delegation" and intent.get("posted_ts"):
        materialize_delegation(intent)


def reconcile_posting_intents() -> int:
    """Bounded lazy-recovery pass over ``posting`` intents. Returns count resolved."""
    resolved = 0
    for intent in list_intents():
        if intent["status"] != "posting":
            continue
        try:
            if reconcile_intent(intent) is not None:
                resolved += 1
        except OutboundError:
            continue
    return resolved


# --- pruning (lazy; terminal intents + terminal records) -------------------

def prune(retention_seconds: int = PRUNE_RETENTION_SECONDS) -> dict[str, int]:
    """Remove terminal intents and terminal/expired records older than retention.

    Retention below the 24h floor is clamped up (never honored) — terminal
    state is crash-recovery memory.
    """
    retention = max(int(retention_seconds), PRUNE_RETENTION_FLOOR_SECONDS)
    now = _now()
    removed = {"intents": 0, "delegations": 0}

    # Retain, per tuple, the single highest-seq intent (ties broken by the most
    # recent update) as the retry_seq watermark; keeping it makes next_retry_seq
    # monotonic across prunes so a re-mint can never reuse a nonce a stale
    # receipt might still reconcile against.
    all_intents = list_intents()
    watermark_nonce: dict[tuple[str, ...], str] = {}
    watermark_rank: dict[tuple[str, ...], tuple[int, float]] = {}
    for intent in all_intents:
        key = _tuple8(intent)
        stamp = _parse_rfc3339(intent.get("updated_at", "")) or _parse_rfc3339(intent.get("created_at", ""))
        rank = (int(intent.get("retry_seq", 0)), stamp.timestamp() if stamp else 0.0)
        if key not in watermark_rank or rank > watermark_rank[key]:
            watermark_rank[key] = rank
            watermark_nonce[key] = intent["nonce"]

    for intent in all_intents:
        if intent["status"] not in _INTENT_TERMINAL:
            continue
        if watermark_nonce.get(_tuple8(intent)) == intent["nonce"]:
            continue  # retain the per-tuple watermark
        updated = _parse_rfc3339(intent.get("updated_at", "")) or _parse_rfc3339(intent.get("created_at", ""))
        if updated is None or (now - updated).total_seconds() <= retention:
            continue
        try:
            os.unlink(_intent_path(intent["nonce"]))
            removed["intents"] += 1
        except OSError:
            pass

    for path, record in list_delegations():
        if record["status"] not in _DELEGATION_TERMINAL:
            continue
        stamp = (
            _parse_rfc3339(record.get("result_claimed_at", ""))
            or _parse_rfc3339(record.get("created_at", ""))
        )
        if stamp is None or (now - stamp).total_seconds() <= retention:
            continue
        try:
            os.unlink(path)
            removed["delegations"] += 1
        except OSError:
            pass
    return removed


# --- current-turn pointer + directory context ------------------------------

def read_current_turn(session: str) -> dict[str, Any] | None:
    """Parse the company current-turn pointer for ``session`` (None if absent)."""
    if not session:
        return None
    data = _read_json(turns_dir() / f"{session}.json")
    if data is None:
        return None
    return parse_current_turn(data)


def _load_context():
    import slack_company_directory as directory
    try:
        dir_data = directory.load_directory()
        bind_data = directory.load_bindings()
    except directory.DirectoryError as exc:
        raise OutboundError(str(exc)) from exc
    agents = {a["name"]: a for a in dir_data.get("agents", [])}
    rooms = {r["name"]: r for r in dir_data.get("rooms", [])}
    bindings = bind_data.get("bindings", [])
    return agents, rooms, bindings


def _session_for_binding(bindings: list[dict[str, Any]], room: str, agent: str) -> str | None:
    for entry in bindings:
        if entry.get("room") == room and entry.get("agent") == agent:
            return entry.get("session")
    return None


def _verify_pointer(session_name: str, origin_ts: str) -> dict[str, Any]:
    """Load + anti-spoof the current-turn pointer for this session."""
    if not session_name:
        raise OutboundError(
            "GC_SESSION_NAME is not set; company verbs need the bound session name")
    turn = read_current_turn(session_name)
    if turn is None:
        raise OutboundError(
            f"no company current-turn pointer for session {session_name!r}; "
            "this session has no active company turn")
    if origin_ts and origin_ts != turn["ts"]:
        raise OutboundError(
            f"--origin-ts {origin_ts!r} does not match the current turn ts "
            f"{turn['ts']!r}; a newer wake overwrote the pointer — pass "
            f"--origin-ts {turn['ts']} to act on that turn")
    if turn["session"] != session_name:
        raise OutboundError(
            f"current-turn pointer session {turn['session']!r} does not match "
            f"GC_SESSION_NAME {session_name!r} (spoof guard)")
    return turn


def _verify_binding(rooms, bindings, turn: dict[str, Any], session_name: str) -> None:
    room = turn["room"]
    agent = turn["agent"]
    bound = _session_for_binding(bindings, room, agent)
    if bound != session_name:
        raise OutboundError(
            f"(room={room}, agent={agent}) is not bound to session "
            f"{session_name!r} (bound to {bound!r}); refusing to act (spoof guard)")
    if room not in rooms:
        raise OutboundError(f"room {room!r} is not in the company directory")


# --- verb: delegate --------------------------------------------------------

def run_delegate(*, to: str, body: str, origin_ts: str, session_name: str) -> dict[str, Any]:
    """Post ``<@to> body`` into the human root's thread as the acting agent.

    Durably persists a posting intent before the POST, enforces one pending
    delegation per tuple under the ``dtuple`` lock, materializes the record on
    success, and reconciles-before-repost on a transient failure (never
    reposts on ambiguity).
    """
    if not body.strip():
        raise OutboundError("--body/--body-file must be non-empty")

    # Every company verb runs lazy recovery + pruning first.
    reconcile_posting_intents()
    prune()

    turn = _verify_pointer(session_name, origin_ts)
    agents, rooms, bindings = _load_context()
    _verify_binding(rooms, bindings, turn, session_name)

    room = turn["room"]
    source_agent = turn["agent"]
    if to == source_agent:
        raise OutboundError("self-targeting is rejected")
    src = agents.get(source_agent)
    if src is None:
        raise OutboundError(f"acting agent {source_agent!r} is not in the directory")
    tgt = agents.get(to)
    if tgt is None:
        raise OutboundError(f"target agent {to!r} is not in the company directory")
    room_rec = rooms[room]
    if to not in (room_rec.get("members") or []):
        raise OutboundError(f"agent {to!r} is not a member of room {room!r}")
    if to not in (room_rec.get("mention_wake") or []):
        raise OutboundError(f"agent {to!r} is not mention-eligible in room {room!r}")

    team = turn["team_id"]
    channel = turn["channel_id"]
    human_root_ts = turn["thread_root_ts"]
    body_hex = body_sha256(body)
    responder_bot = _validate_bot_user_id(tgt["bot_user_id"], role="delegation target")
    requester_bot = _validate_bot_user_id(src["bot_user_id"], role="delegation requester")

    lock_name = dtuple_lock_name(team, channel, human_root_ts, responder_bot, requester_bot)
    with acquire_lock(lock_name):
        pending = find_pending_delegation(team, channel, human_root_ts, responder_bot, requester_bot)
        if pending is not None:
            _, rec = pending
            raise OutboundError(
                f"a pending delegation to {to!r} already exists for this thread "
                f"(record {delegation_filename(team, channel, rec['ts'])}); "
                "await its result or `gc slack delegate --cancel --to " + to + "`")

        tuple8 = (
            src["app_id"], requester_bot, to, responder_bot,
            team, channel, human_root_ts, body_hex,
        )
        inflight = find_nonterminal_intent_for_tuple(tuple8)
        if inflight is not None and inflight["status"] == "posting":
            # A prior attempt reached the provider POST: reconcile against the
            # receipts, never repost on ambiguity (chat.postMessage isn't
            # idempotent).
            resolved = reconcile_intent(inflight)
            if resolved is not None:
                return _delegate_report("published_resumed", resolved, team, channel)
            return _delegate_report("parked", inflight, team, channel)

        if inflight is not None and inflight["status"] == "prepared":
            # A crash between create_intent_once and mark_posting left a
            # prepared intent: prepared provably precedes any provider POST, so
            # adopt it and resume posting rather than parking it forever.
            intent = inflight
        else:
            retry_seq = next_retry_seq(tuple8)
            nonce = compute_nonce(
                source_app_id=src["app_id"], source_bot_user_id=requester_bot,
                target_agent=to, target_bot_user_id=responder_bot,
                team_id=team, channel_id=channel, human_root_ts=human_root_ts,
                body_sha256=body_hex, retry_seq=retry_seq,
            )
            intent = build_intent(
                nonce=nonce, retry_seq=retry_seq,
                source_agent=source_agent, source_app_id=src["app_id"],
                source_bot_user_id=requester_bot, target_agent=to,
                target_bot_user_id=responder_bot, team_id=team, channel_id=channel,
                room=room, human_root_ts=human_root_ts,
                requester_session=session_name, body_hex=body_hex,
            )
            intent, _created = create_intent_once(intent)

        nonce = intent["nonce"]
        token = load_bot_token(source_agent)
        intent = intent_mark_posting(nonce)
        text = compose_delegation_text(responder_bot, body)
        meta = delegation_metadata(nonce, human_root_ts, source_agent, to)
        try:
            posted_ts = post_message(
                token, channel=channel, text=text,
                thread_ts=human_root_ts, metadata=meta,
                max_attempts=int(intent.get("max_attempts", DEFAULT_MAX_ATTEMPTS)))
        except DefinitivePostError as exc:
            intent_mark_failed(nonce, str(exc))
            raise OutboundError(f"delegation post failed (definitive): {exc}") from exc
        except TransientPostError as exc:
            resolved = reconcile_intent(intent)
            if resolved is not None:
                return _delegate_report("published_resumed", resolved, team, channel)
            return _delegate_report("parked", intent, team, channel, note=str(exc))

        intent = intent_mark_published(nonce, posted_ts)
        materialize_delegation(intent)
        return _delegate_report("published", intent, team, channel)


def _delegate_report(status: str, intent: dict[str, Any], team: str, channel: str,
                     note: str = "") -> dict[str, Any]:
    posted_ts = intent.get("posted_ts", "")
    report: dict[str, Any] = {
        "status": status,
        "nonce": intent["nonce"],
        "target_agent": intent["target_agent"],
        "posted_ts": posted_ts,
    }
    if posted_ts:
        report["delegation_key"] = delegation_filename(team, channel, posted_ts)
    if note:
        report["note"] = note
    return report


# --- verb: delegate --cancel ----------------------------------------------

def run_cancel(*, to: str, origin_ts: str, session_name: str) -> dict[str, Any]:
    """Transition the caller's own pending delegation for the tuple to ``expired``."""
    reconcile_posting_intents()
    prune()

    turn = _verify_pointer(session_name, origin_ts)
    agents, rooms, bindings = _load_context()
    _verify_binding(rooms, bindings, turn, session_name)

    source_agent = turn["agent"]
    src = agents.get(source_agent)
    tgt = agents.get(to)
    if src is None or tgt is None:
        raise OutboundError(f"unknown agent in cancel (source={source_agent!r}, target={to!r})")

    team = turn["team_id"]
    channel = turn["channel_id"]
    human_root_ts = turn["thread_root_ts"]
    responder_bot = tgt["bot_user_id"]
    requester_bot = src["bot_user_id"]

    lock_name = dtuple_lock_name(team, channel, human_root_ts, responder_bot, requester_bot)
    with acquire_lock(lock_name):
        target: tuple[pathlib.Path, dict[str, Any]] | None = None
        for path, record in list_delegations():
            if (
                record["team_id"] == team
                and record["channel_id"] == channel
                and record["thread_root_ts"] == human_root_ts
                and record["expected_responder_bot_user_id"] == responder_bot
                and record["requester_bot_user_id"] == requester_bot
                and record["status"] == "pending"
            ):
                target = (path, record)
                break
        if target is None:
            return {"status": "no_pending", "target_agent": to}
        path, record = target
        expire_delegation(path, record)
        return {"status": "cancelled", "target_agent": to, "delegation_key": path.name}


# --- reply-current company path (peer result / synthesis / root reply) ------

def _durable_company_post(
    *,
    op: str,
    source_agent: str,
    source_app_id: str,
    source_bot_user_id: str,
    target_agent: str,
    target_bot_user_id: str,
    team: str,
    channel: str,
    room: str,
    human_root_ts: str,
    requester_session: str,
    body: str,
    text: str,
    thread_ts: str,
    token: str,
    metadata_nonce: str,
    make_metadata,
) -> tuple[dict[str, Any], str]:
    """Post ``text`` through a durable intent, keyed by the (source, target,
    body) tuple. Returns ``(intent, outcome)`` where outcome is one of
    ``posted`` (fresh POST), ``reused`` (already posted / reconciled — no
    repost), or ``parked`` (transient failure; recovery on the next verb).

    The same reconcile-before-repost discipline as delegations: the intent is
    created before the provider POST, a timeout/5xx reconciles against the
    receipt scan (never a blind repost), and a definitive 4xx fails the intent.
    """
    body_hex = body_sha256(body)
    tuple8 = (
        source_app_id, source_bot_user_id, target_agent, target_bot_user_id,
        team, channel, human_root_ts, body_hex,
    )

    existing = reusable_intent_for_tuple(tuple8)
    if existing is not None and existing["status"] == "published":
        return existing, "reused"
    if existing is not None and existing["status"] == "posting":
        resolved = reconcile_intent(existing)
        return (resolved, "reused") if resolved is not None else (existing, "parked")

    if existing is not None and existing["status"] == "prepared":
        # Crash between create and mark_posting: prepared precedes any POST, so
        # adopt it and resume.
        intent = existing
    else:
        retry_seq = next_retry_seq(tuple8)
        nonce = compute_nonce(
            source_app_id=source_app_id, source_bot_user_id=source_bot_user_id,
            target_agent=target_agent, target_bot_user_id=target_bot_user_id,
            team_id=team, channel_id=channel, human_root_ts=human_root_ts,
            body_sha256=body_hex, retry_seq=retry_seq,
        )
        intent = build_intent(
            nonce=nonce, retry_seq=retry_seq, source_agent=source_agent,
            source_app_id=source_app_id, source_bot_user_id=source_bot_user_id,
            target_agent=target_agent, target_bot_user_id=target_bot_user_id,
            team_id=team, channel_id=channel, room=room,
            human_root_ts=human_root_ts, requester_session=requester_session,
            body_hex=body_hex, op=op, metadata_nonce=metadata_nonce or nonce,
        )
        intent, _created = create_intent_once(intent)
        if intent["status"] == "published":
            return intent, "reused"
        if intent["status"] == "posting":
            resolved = reconcile_intent(intent)
            return (resolved, "reused") if resolved is not None else (intent, "parked")

    nonce = intent["nonce"]
    effective_nonce = intent.get("metadata_nonce") or nonce
    intent = intent_mark_posting(nonce)
    meta = make_metadata(effective_nonce)
    try:
        posted_ts = post_message(
            token, channel=channel, text=text, thread_ts=thread_ts, metadata=meta,
            max_attempts=int(intent.get("max_attempts", DEFAULT_MAX_ATTEMPTS)))
    except DefinitivePostError as exc:
        intent_mark_failed(nonce, str(exc))
        raise OutboundError(f"{op} post failed (definitive): {exc}") from exc
    except TransientPostError:
        resolved = reconcile_intent(intent)
        if resolved is not None:
            return resolved, "reused"
        current = _read_json(_intent_path(nonce))
        return (parse_intent(current) if current is not None else intent), "parked"

    intent = intent_mark_published(nonce, posted_ts)
    return intent, "posted"


def _company_post_report(
    intent: dict[str, Any], outcome: str, *, kind: str, delegation_key: str = "",
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "posted" if outcome in ("posted", "reused") else "parked",
        "kind": kind,
        "nonce": intent["nonce"],
        "posted_ts": intent.get("posted_ts", ""),
    }
    if delegation_key:
        report["delegation_key"] = delegation_key
    if outcome == "reused":
        report["note"] = "reconciled to the existing post; not reposted"
    elif outcome == "parked":
        report["note"] = ("post parked on a transient failure; recovery is automatic "
                          "once the receipt lands")
    return report


def post_peer_result(*, body: str, origin_ts: str, session_name: str) -> dict[str, Any]:
    """Post a delegation result into the human root's thread (responder → requester).

    The acting agent is the delegation's expected responder; the result posts
    with its own token, mentioning only the recorded requester, and carries the
    result metadata gate (``gc_delegation_result`` + nonce + delegation_ts).
    Routed through a durable intent (op=result): created before the POST,
    reconciled-before-repost on a timeout/5xx, failed on a definitive 4xx.
    """
    if not body.strip():
        raise OutboundError("--body/--body-file must be non-empty")
    reconcile_posting_intents()
    prune()

    turn = _verify_pointer(session_name, origin_ts)
    if turn["kind"] != "peer_delegation":
        raise OutboundError(f"current turn kind is {turn['kind']!r}, not peer_delegation")
    agents, rooms, bindings = _load_context()
    _verify_binding(rooms, bindings, turn, session_name)

    data = _read_json(delegations_dir() / turn["delegation_key"])
    if data is None:
        raise OutboundError(f"delegation record {turn['delegation_key']!r} is missing or corrupt")
    record = parse_delegation(data)

    acting = turn["agent"]
    if acting != record["expected_responder_agent"]:
        raise OutboundError(
            f"session agent {acting!r} is not the expected responder "
            f"{record['expected_responder_agent']!r} for this delegation")
    src = agents.get(acting)
    if src is None:
        raise OutboundError(f"acting agent {acting!r} is not in the directory")

    token = load_bot_token(acting)
    requester_bot = record["requester_bot_user_id"]
    # compose_result_text validates the requester id before interpolation.
    text = compose_result_text(requester_bot, body)
    delegation_nonce = record["nonce"]
    delegation_ts = record["ts"]
    intent, outcome = _durable_company_post(
        op="result",
        source_agent=acting, source_app_id=src["app_id"],
        source_bot_user_id=src["bot_user_id"],
        target_agent=record["requester_agent"], target_bot_user_id=requester_bot,
        team=turn["team_id"], channel=turn["channel_id"], room=turn["room"],
        human_root_ts=record["thread_root_ts"], requester_session=session_name,
        body=body, text=text, thread_ts=record["thread_root_ts"], token=token,
        metadata_nonce=delegation_nonce,
        make_metadata=lambda n: result_metadata(n, delegation_ts),
    )
    return _company_post_report(
        intent, outcome, kind="peer_result", delegation_key=turn["delegation_key"])


def post_peer_synthesis(*, body: str, origin_ts: str, session_name: str) -> dict[str, Any]:
    """Post a synthesis into the human root thread with no live agent mentions.

    Routed through a durable intent (op=synthesis) so a timed-out synthesis
    post is reconciled against its own echo rather than reposted.
    """
    if not body.strip():
        raise OutboundError("--body/--body-file must be non-empty")
    reconcile_posting_intents()
    prune()

    turn = _verify_pointer(session_name, origin_ts)
    if turn["kind"] != "peer_result":
        raise OutboundError(f"current turn kind is {turn['kind']!r}, not peer_result")
    agents, rooms, bindings = _load_context()
    _verify_binding(rooms, bindings, turn, session_name)

    acting = turn["agent"]
    src = agents.get(acting)
    if src is None:
        raise OutboundError(f"acting agent {acting!r} is not in the directory")
    token = load_bot_token(acting)
    text = compose_synthesis_text(body)
    intent, outcome = _durable_company_post(
        op="synthesis",
        source_agent=acting, source_app_id=src["app_id"],
        source_bot_user_id=src["bot_user_id"],
        # No mention: key the tuple on the acting identity itself.
        target_agent=acting, target_bot_user_id=src["bot_user_id"],
        team=turn["team_id"], channel=turn["channel_id"], room=turn["room"],
        human_root_ts=turn["thread_root_ts"], requester_session=session_name,
        body=body, text=text, thread_ts=turn["thread_root_ts"], token=token,
        metadata_nonce="",
        make_metadata=synthesis_metadata,
    )
    return _company_post_report(intent, outcome, kind="peer_synthesis")


def post_company_root_reply(*, body: str, origin_ts: str, session_name: str) -> dict[str, Any]:
    """Post an ordinary reply into the company room's thread root.

    For ambient/targeted/peer_input turns (a human message or an uncorrelated
    peer wake): the acting agent answers into ``thread_root_ts`` with its own
    token and no live mentions (escaped body, no gc metadata). No delegation
    record is involved. This replaces the legacy resolution, which the company
    delivery path never feeds.
    """
    if not body.strip():
        raise OutboundError("--body/--body-file must be non-empty")
    reconcile_posting_intents()
    prune()

    turn = _verify_pointer(session_name, origin_ts)
    if turn["kind"] not in ("ambient", "targeted", "peer_input"):
        raise OutboundError(
            f"current turn kind is {turn['kind']!r}, not a room-reply kind")
    agents, rooms, bindings = _load_context()
    _verify_binding(rooms, bindings, turn, session_name)

    acting = turn["agent"]
    if acting not in agents:
        raise OutboundError(f"acting agent {acting!r} is not in the directory")
    token = load_bot_token(acting)
    text = compose_synthesis_text(body)  # escaped, no live mention
    posted_ts = post_message(
        token, channel=turn["channel_id"], text=text,
        thread_ts=turn["thread_root_ts"])
    return {"status": "posted", "kind": turn["kind"], "posted_ts": posted_ts}


# --- CLI entry point -------------------------------------------------------

def _load_body_arg(body: str, body_file: str) -> str:
    if body and body_file:
        raise OutboundError("pass --body OR --body-file, not both")
    if body_file:
        try:
            return pathlib.Path(body_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise OutboundError(f"cannot read --body-file {body_file}: {exc}") from exc
    if body:
        return body
    raise OutboundError("either --body or --body-file is required")


def cmd_delegate(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="slack delegate")
    parser.add_argument("--to", required=True, help="Target agent name (slug)")
    parser.add_argument("--body", default="")
    parser.add_argument("--body-file", default="")
    parser.add_argument("--cancel", action="store_true",
                        help="Expire the caller's own pending delegation to --to")
    parser.add_argument("--origin-ts", default="",
                        help="Pin a specific turn ts when a newer wake overwrote the pointer")
    args = parser.parse_args(argv)

    session_name = os.environ.get("GC_SESSION_NAME", "").strip()
    if args.cancel:
        result = run_cancel(to=args.to, origin_ts=args.origin_ts, session_name=session_name)
    else:
        body = _load_body_arg(args.body, args.body_file)
        result = run_delegate(
            to=args.to, body=body, origin_ts=args.origin_ts, session_name=session_name)
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("status") == "parked":
        print(
            f"note: delegation parked (nonce {result.get('nonce')}); not reposting on "
            "ambiguity — recovery is automatic once the post's receipt lands",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("error: expected a subcommand (delegate)", file=sys.stderr)
        return 2
    sub, rest = argv[0], argv[1:]
    try:
        if sub == "delegate":
            return cmd_delegate(rest)
        print(f"error: unknown subcommand {sub!r}", file=sys.stderr)
        return 2
    except OutboundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

