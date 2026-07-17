# Slack Company Rooms — Implementation Plan

Companion to `slack-full/docs/company-rooms.md` (v2). Reference
implementation: the Discord company-rooms branch
(`feat/discord-company-rooms`), whose behavioral contract this pack
reproduces on Slack.

Verification bar for every phase (matches CI; each line runs from the repo
root):

```sh
(cd slack-full/adapter && gofmt -l . && go vet ./... && go test -race ./...)
(cd slack-full/cli && go test -race ./...)
python3 -m pytest slack-full/tests -q
```

## Phase overview

| Phase | Scope | Acceptance rules |
| --- | --- | --- |
| 1 | Durable admission, company directory + bindings, deterministic human routing | 1, 2, 4 (non-delegation legs), 6, 8, 9, 10, 11 |
| 2a | Agent identity app provisioning (manifest template, install, harvest, secrets) | pilot checklist |
| 2 | Per-agent tokens, delegation, results, trust envelope, hydration | 3, 5, 7, remaining legs of 4 |
| 3 | Sibling synthesis snapshots, redrive parity, recovery ordering, visible acks | replay/ordering proofs |
| 4 | Per-agent DMs | 12 |
| 5 | Durable-request-ledger `/v0` integration | blocked on gascity drafts landing |

Company-room behavior stays additive throughout: nothing changes until
`gc slack import-company-directory` has produced a valid directory registry,
and non-company rooms keep legacy behavior in every phase.

---

## Phase 1 — durable admission + directory + human routing

Sub-phases 1a–1c are independent new files (parallelizable). 1d wires them
into the adapter (sequential, after 1a–1c). 1e is the Python/CLI surface
(parallel with 1d).

Delivery guarantee in this phase (stated honestly): at-least-once with
receipt-side suppression. Each session submission carries
`Idempotency-Key: ingress:<id>:target:<session>` as an HTTP header, but
gc's idempotency cache is in-memory with a bounded TTL, so the durable
receipt is the authority; a crash between gc submission and the receipt's
delivery record may duplicate that one target. Phase 5 upgrades this.

### Normalized registries (shared contract for 1a and 1e)

Two CLI-written JSON files, resolved exactly like the existing registries
(same directory as `apps.json`), each with an env-var path override
following the existing per-registry override convention (add
`SLACK_COMPANY_DIRECTORY_PATH`, `SLACK_COMPANY_BINDINGS_PATH`, and
`SLACK_COMPANY_INGRESS_DIR`, mirroring the style of the existing overrides
around `main.go:497-507`; the Python CLI resolves the identical paths).
Both are staged/committed **outside** the six-registry atomic reload with
their own last-known-good retention, so an invalid company file never
blocks the other registries' SIGHUP.

`company_directory.json` — written by `import-company-directory`; wildcards
expanded at import time; the Go loader re-validates and fails closed:

```json
{
  "schema_version": 1,
  "source_sha256": "<hex of the imported TOML>",
  "imported_at": "<RFC3339>",
  "agents": [
    {"name": "ollie", "app_id": "A0AAAAAA1", "bot_user_id": "U0AAAAAA1"}
  ],
  "rooms": [
    {
      "name": "orchestrator-team",
      "team_id": "T0AAAAAAA",
      "channel_id": "C0AAAAAAA",
      "members": ["ollie", "riley"],
      "ambient_wake": ["ollie"],
      "mention_wake": ["ollie", "riley"]
    }
  ]
}
```

`company_bindings.json` — written by `bind-company-agent`; singleton by
construction:

```json
{
  "schema_version": 1,
  "bindings": [
    {"room": "orchestrator-team", "agent": "ollie", "session": "ollie-main"}
  ]
}
```

Validation (both sides, fail closed): lowercase slug names; unique names,
`app_id`s, `bot_user_id`s, room names, `(team_id, channel_id)` pairs;
`ambient_wake ⊆ members`, `mention_wake ⊆ members`; wildcard permitted in
TOML `members`/`mention_wake` only (never `ambient_wake`), absent from the
normalized JSON; unknown agent references invalid; at most one binding per
(room, agent); binding room/agent must exist in the directory at load time
(a binding referencing a missing room/agent is dropped with a warning, not
fatal); empty `agents`/`rooms`/`bindings` valid.

### 1a — `adapter/company_directory.go`, `adapter/company_bindings.go` (+ `_test.go`)

```go
type CompanyAgent struct {
    Name      string `json:"name"`
    AppID     string `json:"app_id"`
    BotUserID string `json:"bot_user_id"`
}

type CompanyRoom struct {
    Name        string   `json:"name"`
    TeamID      string   `json:"team_id"`
    ChannelID   string   `json:"channel_id"`
    Members     []string `json:"members"`
    AmbientWake []string `json:"ambient_wake"`
    MentionWake []string `json:"mention_wake"`
}

type CompanyDirectory struct { /* parsed + normalized indexes */ }

func ParseCompanyDirectory(data []byte) (*CompanyDirectory, error)

func (d *CompanyDirectory) RoomByChannel(teamID, channelID string) (*CompanyRoom, bool)
func (d *CompanyDirectory) AgentByBotUserID(id string) (*CompanyAgent, bool)
func (d *CompanyDirectory) AgentByName(name string) (*CompanyAgent, bool)
func (d *CompanyDirectory) IsMember(room *CompanyRoom, agent string) bool
func (d *CompanyDirectory) IsMentionEligible(room *CompanyRoom, agent string) bool

type CompanyBindings struct { /* (room, agent) -> session index */ }

func ParseCompanyBindings(data []byte, dir *CompanyDirectory) (*CompanyBindings, warnings []string, err error)
func (b *CompanyBindings) SessionFor(room, agent string) (string, bool)
```

Snapshot holders (`companyDirectoryStore`, `companyBindingsStore`) with
`Load(path)` / `Snapshot()` / `StageReload(path)` semantics. **Deliberate
divergence from the other registries**: `Load` at startup is never fatal —
a corrupt or invalid file logs the validation error and installs a nil
snapshot (company routing disabled) while the adapter keeps serving legacy
traffic. Reload failure keeps last-known-good. Nil snapshot = no company
routing.

Tests: every validation rule (table-driven); duplicate/unknown references
fail; corrupt file at construction → nil snapshot + surfaced error,
process viable; last-known-good retention on bad reload; binding referencing
removed room/agent dropped with warning; empty registries inert.

### 1b — `adapter/ingress_receipts.go` (+ `_test.go`)

Durable receipt store rooted at `<GC_CITY_PATH>/.gc/slack/chat-ingress/`
(fallback `/tmp/gc-slack-adapter/chat-ingress`, mirroring
`thread_sessions.json` path resolution; `SLACK_COMPANY_INGRESS_DIR`
override), files 0600 in 0700 dirs.

```go
type ReceiptOrigin struct{ TeamID, ChannelID, TS string }

type TargetDelivery struct {
    Session        string    `json:"session"`
    Kind           string    `json:"kind"`   // "ambient" | "targeted"
    Status         string    `json:"status"` // "pending" | "delivered" | "failed"
    IdempotencyKey string    `json:"idempotency_key"` // ingress:<id>:target:<session>
    Attempts       int       `json:"attempts"`
    UpdatedAt      time.Time `json:"updated_at"`
    Detail         string    `json:"detail,omitempty"`
}

type IngressReceipt struct {
    ID          string        `json:"id"` // "in-" + sanitized origin
    Generation  int64         `json:"generation"`
    Origin      ReceiptOrigin `json:"origin"`
    EventID     string        `json:"event_id"`
    APIAppID    string        `json:"api_app_id"`
    RetryNum    int           `json:"retry_num"`
    RetryReason string        `json:"retry_reason,omitempty"`
    ReceivedAt  time.Time     `json:"received_at"`
    Status      string        `json:"status"` // "received" | "routing" | "delivered" | "no_delivery" | "failed"
    // Event is the COMPLETE inner Slack event object as received —
    // routing (text/blocks/thread_ts) and crash replay depend on it.
    Event   json.RawMessage           `json:"event"`
    Targets map[string]TargetDelivery `json:"targets,omitempty"`
    Reason  string                    `json:"reason,omitempty"` // parked/no_delivery/failed detail
}

func NewIngressReceiptStore(dir string) (*IngressReceiptStore, error)

// Admit is the linearization point, claim-and-content atomic: the full
// receipt is written to a temp file in the same directory, fsynced, then
// hard-linked (os.Link) to the final origin-keyed name; EEXIST = duplicate;
// the directory is fsynced and the temp name removed. An existing but
// unparseable receipt file is quarantined (*.corrupt) and the link retried
// once; persistent failure returns err (caller answers 503). There is no
// state in which a claim exists without content.
func (s *IngressReceiptStore) Admit(r *IngressReceipt) (created bool, existing *IngressReceipt, err error)

// Update performs a generation-checked atomic rewrite (temp+rename+fsync
// of file and directory — same durability as Admit). If the on-disk
// generation differs from r.Generation, ErrStale is returned; the caller
// re-reads and merges. Update increments the generation it writes.
func (s *IngressReceiptStore) Update(r *IngressReceipt) error

func (s *IngressReceiptStore) Get(origin ReceiptOrigin) (*IngressReceipt, error)

// Pending returns non-terminal receipts ordered by (ReceivedAt, Origin.TS).
func (s *IngressReceiptStore) Pending() ([]*IngressReceipt, error)

// Sweep removes terminal receipts older than retention. Retention is
// pinned at 7 days (Discord parity); values below 24h are rejected —
// terminal receipts are the dedup memory for Slack's Delayed Events
// redelivery horizon. Non-terminal receipts are never swept.
func (s *IngressReceiptStore) Sweep(retention time.Duration) (removed int, err error)

// WriteFailures exposes a monotonic counter of failed Admit/Update
// persistence attempts for the gateway status payload / healthz detail.
func (s *IngressReceiptStore) WriteFailures() uint64
```

Filenames derive from the origin via a `safe_storage_id`-style sanitizer
(hash long/hostile components). Terminal states: `delivered`,
`no_delivery`, `failed` (per-target detail preserved). Parked receipts are
non-terminal (`Status: "received"`, `Reason: "parked_no_directory_room"`).

Tests: concurrent Admit first-writer-wins (`-race`); duplicate returns
existing; **crash between temp write and link** (simulate by leaving temp
file) admits cleanly on retry; corrupt existing receipt quarantined then
claimed; Update generation conflict returns ErrStale; un-fsynced-rename
crash simulation (temp present, final intact); Pending ordering; Sweep
respects retention floor, never sweeps non-terminal, quarantines corrupt
scan entries.

### 1c — `adapter/company_routing.go` (+ `_test.go`)

Pure functions, no I/O.

```go
type AuthorClass int

const (
    AuthorHuman AuthorClass = iota
    AuthorCompanyBot // resolution to a single registered agent happens in Phase 2
    AuthorBot        // any bot/webhook/integration author (no delivery in Phase 1)
    AuthorSelf
)

// AdmissibleSubtype reports whether a message subtype is admissible:
// "" (absent), "file_share", "thread_broadcast", "bot_message".
func AdmissibleSubtype(subtype string) bool

type CompanyMessage struct {
    TeamID, ChannelID, TS, ThreadTS string
    UserID, BotID                   string
    Subtype                         string
    Text                            string
    Blocks                          json.RawMessage
}

type WakeTarget struct {
    Agent CompanyAgent
    Kind  string // "ambient" | "targeted"
}

type RouteDecision struct {
    Room   *CompanyRoom
    Author AuthorClass
    Wakes  []WakeTarget
    Reason string // machine-readable no-delivery reason
}

// ExtractMentionIDs returns the UNION of user IDs found as rich_text
// "user" elements in blocks and as canonical <@U…> / <@U…|label> tokens in
// text. Neither source alone is sufficient: Slack guarantees rich_text
// only for end-user client messages, and bot-composed messages may carry
// mentions only as text tokens.
func ExtractMentionIDs(blocks json.RawMessage, text string) []string

func ComputeWakeSet(dir *CompanyDirectory, msg CompanyMessage, selfBotUserID string) RouteDecision
```

Rules implemented exactly (design doc routing table): mention exclusivity;
ambient set for unmentioned human messages; the subtype allowlist gate
(non-allowlisted subtypes are handled before this function, but
`ComputeWakeSet` re-checks and returns no wakes with reason
`subtype_not_admissible` as defense in depth); bot-authored messages
(`bot_message` subtype or `BotID != ""`) return **no wakes in Phase 1**
with reasons distinguishing `company_bot_phase2` / `unknown_bot` /
`company_self`; non-company channels return `Room == nil` (caller falls
through to legacy); mentioned-but-ineligible or non-member IDs add no wake
(recorded in Reason). Wake set deduplicated.

Tests: table-driven port of acceptance rules 1, 2, 4, 6, plus: mention
extraction from rich_text alone, text tokens alone (`<@U…>` and
`<@U…|label>` forms), union of both, literal `@riley` never matches,
`channel_join`/`channel_topic` subtypes produce no wakes, self messages
produce no wakes.

### 1d — adapter wiring (`main.go`, `company_ingress_test.go`)

- Load both company registries at startup (never fatal — nil snapshot on
  error) and stage/commit them on SIGHUP **outside** the six-registry
  atomic set, with their own last-known-good log lines.
- `handleSlackEvents` changes, inserted between the `url_verification`
  return (~main.go:1917-1921) and the current unconditional
  `w.WriteHeader(StatusOK)` (~main.go:1924):
  - Synchronously decode the inner event when the envelope is an
    `event_callback` (the current code defers this to the async goroutine;
    the company path needs channel/ts/subtype before acking). Extend the
    event struct with `Blocks json.RawMessage`.
  - If `(team_id, channel)` matches a directory room: apply the
    admissibility gate (allowlist; non-admissible → 200, no receipt;
    unkeyable → log + 200); capture `X-Slack-Retry-Num`/`-Reason` from
    `r.Header`; `Admit`; duplicate → 200; created → trigger delivery and
    200; store failure → 503 **without** `x-slack-no-retry`.
  - Non-company events keep today's path byte-for-byte.
  - Admission never depends on `acquireDispatchSlot`: the semaphore gates
    only the async delivery trigger; if no slot is free the receipt simply
    stays pending for the sweep (backpressure, not drop).
- Company delivery worker: in-process single-flight per receipt ID;
  claims the receipt (`Status: routing` via generation-checked Update);
  computes the wake set (1c) only when the receipt has no recorded
  targets — recorded targets are a frozen route that redrives drive to
  terminal states, never recompute (`no_delivery` is legal only with no
  recorded targets and an empty freshly-computed wake set); resolves each
  wake through the company-bindings snapshot (missing binding → target
  `failed` with reason, no legacy fallback); delivers each target via a
  new `deliverToCompanySession` helper — POST
  `/v0/city/{city}/session/{session}/messages` with header
  `Idempotency-Key: ingress:<id>:target:<session>`, an explicit
  per-request timeout, and the system-reminder-style envelope used by
  alias dispatch (markup-neutralized); marks a target `delivered` only on
  gc's acknowledged 2xx; leaves it `pending` (attempts++) on
  timeout/5xx/408/429/connection error for sweep retry with the same
  key; marks it `failed` on any other 4xx or when the bounded attempts
  cap is exhausted; sets the terminal receipt status when all targets
  resolve. `app_mention` events for company-room channels are owned by
  the company path (200, no receipt, no legacy dispatch).
- Parking: a pending receipt whose channel matches no *current* directory
  room (including nil directory) is left pending with
  `Reason: parked_no_directory_room` — never terminally resolved, never
  legacy-delivered; the sweep retries it after every directory change.
- Startup recovery barrier, company-scoped: legacy routes, `/healthz`,
  interactions, and the internal listener serve immediately; company-room
  admissible events get 503 (retryable) until one synchronous
  `Pending()` scan completes and its receipts are enqueued. (Posting
  intents precede pending ingress in the barrier ordering from Phase 2
  on.) `/healthz` detail is the sole adapter status surface: barrier
  state, receipt-store health and `WriteFailures()`, delivery-failure
  count, and directory/bindings snapshot state. Membership warnings
  surface only via the Python `import-company-directory`/`peers` path in
  Phase 1. A receipt-store construction failure at startup puts the
  gateway in degraded mode — company-admissible events 503 (never
  legacy), `/healthz` reports the store error, and construction is
  retried — it is never a silent fallthrough to legacy.
- Periodic sweep (60s) starts after one successful barrier pass, waits one
  interval first, respects the routing-claim stale-reclaim window (skip
  receipts whose `UpdatedAt` is fresher than 5 minutes unless terminal).
- Company rooms bypass legacy generic fanout and alias dispatch
  deterministically.

Integration tests (`company_ingress_test.go`, httptest): acceptance 8
(admit, crash before delivery → restart delivers exactly once; crash
after submit before record → second delivery attempt carries the same
Idempotency-Key), 9 (retry-num redelivery → 200, no second delivery), 10
(store failure → 503 without no-retry header + counter increment;
subsequent retry admits), 11 (parked receipt: directory removed → stays
pending; restored → delivers), ambient and mention routing end-to-end,
saturation backpressure (no slot → receipt pending, sweep delivers),
legacy path untouched for non-company channels.

### 1e — CLI surface (Python)

New script `slack-full/scripts/slack_company_directory.py`: TOML parse
(`tomllib`), full validation, wildcard expansion, normalized JSON emission
(atomic tmp+rename) for both registries, `source_sha256`, membership
verification via `conversations.info`/`conversations.members` (warnings,
not failures, when the token lacks scopes or the bot is not a member),
`peers` listing (rooms, members, wake policy, bindings, membership
warnings). Command wrappers follow the shipped convention —
`commands/<verb>.sh` at the top level plus `commands/<verb>/command.toml`
(with `run = "../<verb>.sh"`) and `commands/<verb>/help.md` — for three
verbs: `import-company-directory`, `bind-company-agent`, `peers`. No
`pack.toml` registration exists or is needed (verb discovery is
directory-driven). Tests (`tests/test_slack_company_directory.py`):
validation table mirrored from 1a, TOML→JSON golden file, idempotent
re-import, invalid file leaves the existing registry untouched, bind
create/replace, peers output including membership warnings (Slack API
mocked).

---

## Phase 2a — agent identity app provisioning

Manifest template for agent identity apps (`manifest/agent-app.json` +
README section): bot user, `chat:write`, `app_home.messages_tab_enabled:
true`, `messages_tab_read_only_enabled: false`, interactivity off, **no
event subscriptions** (the DM phase adds `message.im` + `im:history`).
Switchboard manifest (`manifest/app.json`) gains `channels:read`,
`groups:read` (membership checks) and `users:read` (`bots.info` author
resolution) — scope changes require reinstall, called out in the runbook.
Operator steps documented: create/install per agent, harvest `app_id` +
`bot_user_id` into the directory TOML, register each signing secret with
the adapter (existing `import-app` flow), drop each bot token into the
company secrets dir (the token loader refuses files not 0600, dirs not
0700, and symlinks — validation, not trust), invite each member bot to
its rooms. These apps are internal, never Marketplace-distributed
(distribution would demote `conversations.*` rate tiers).

## Phase 2 — identities, delegation, results, hydration

Ownership split (mirrors the Discord reference): **Python owns company
outbound** — intents, per-agent token files, `chat.postMessage`,
delegation-record creation, lazy recovery, pruning — exactly as
`discord_chat_delegate.py` / `discord_intake_common.py` do; **Go owns
ingress** — author resolution, trust checklist, result-claim and expiry
transitions on delegation records, peer envelopes, hydration, the
per-session current-turn pointer. Both sides write shared on-disk state
under the adapter state root; every schema below is the cross-language
contract, validated fail-closed on both sides, serialized by the lock
contract below.

### Shared state contract (pins 2b and 2c)

Paths (env override > `<GC_CITY_PATH>/.gc/slack/<leaf>` >
`/tmp/gc-slack-adapter/<leaf>`, matching Phase 1 conventions):
- `SLACK_COMPANY_SECRETS_DIR` > `secrets/` — token files
  `bot-token-<agent>.txt`, 0600 in 0700 dir, one per directory agent.
- `SLACK_COMPANY_INTENTS_DIR` > `company-delegation-intents/` —
  `<nonce>.json`, written by Python only.
- `SLACK_COMPANY_DELEGATIONS_DIR` > `company-delegations/` — created
  once by Python at publish (O_EXCL/link create-once; `EEXIST` = adopt
  the existing record, never overwrite); status transitions
  (claim/expiry) written by Go, expiry also by Python's lazy pruner.
- `SLACK_COMPANY_TURNS_DIR` > `company-current-turn/` —
  `<session>.json`, written by Go only (the delivery worker), read by
  the Python verbs.
- `SLACK_COMPANY_LOCKS_DIR` > `locks/` — advisory lock files (below).

**Lock contract.** All cross-process critical sections use an advisory
`flock(LOCK_EX)` (`fcntl.flock` in Python, `syscall.Flock` in Go) on
`locks/<label>-<sha256hex(NUL-joined key fields)[:16]>.lock`. Two locks
exist in Phase 2: the **delegation-tuple lock** (label `dtuple`, key =
team, channel, thread_root_ts, responder_bot_user_id,
requester_bot_user_id), held by `delegate` across its
scan → intent-create → post → record-materialize section, by `--cancel`,
by Go's claim transition, and by any expiry rewrite; and the
**intent lock** (label `intent`, key = nonce) for attempts-CAS updates.
Generation checks remain on top of the locks (defense in depth), but
the locks are what make cross-process read-modify-write sound.

**Filename sanitizer (byte-for-byte cross-language spec).** For each
component: bytes outside `[A-Za-z0-9._-]`, a leading `.`, the string
`..`, or length > 64 cause the component to be replaced by
`h<sha256hex(component)[:16]>`; otherwise the component passes through.
Delegation filename: `dg-<san(team)>-<san(channel)>-<san(ts)>-`
`<sha256hex(team NUL channel NUL ts)[:12]>.json` (digest suffix always
present, mirroring Go `receiptID`). 2d ships golden filename fixtures
both suites must reproduce exactly.

**Root derivation (normative).** From the triggering receipt's stored
event: `human_root_ts := Event.thread_ts if non-empty else Event.ts`.
`thread_ts` on `chat.postMessage` must always be a parent — Slack
documents replying to a reply as invalid. 2c's root verifier grants
`root_provenance: human_root_verified` only when the fetched root is a
parent (`thread_ts` absent or equal to `ts`) AND its author is a non-bot
human; otherwise `root_unverified`.

`company-delegation-intents/<nonce>.json` — created BEFORE any provider
POST, for delegations AND for results/synthesis (an `op` field:
`delegation | result | synthesis`; every company post goes through the
intent path so no post can double on a timeout). Nonce = `gcs-` + first
20 hex of sha256 over the canonical anticipated record: (source app_id,
source bot_user_id, target agent, target bot_user_id, team, channel,
human_root_ts, body_sha256, retry_seq) — `retry_seq` is monotonic:
`max(retry_seq of existing intents for the tuple) + 1` when minting
fresh (never a file count, so pruning cannot re-mint a used nonce), and
the pruner always retains the highest-seq intent per tuple. Crash
retries of one logical delegation resume the existing intent; successive
logical delegations mint fresh nonces:

```json
{
  "schema_version": 1,
  "nonce": "gcs-<20hex>",
  "retry_seq": 0,
  "status": "prepared",
  "attempts": 0, "max_attempts": 3,
  "created_at": "<RFC3339>", "updated_at": "<RFC3339>",
  "retry_deadline": "<RFC3339: first attempt + 120s>",
  "ttl_seconds": 86400,
  "source_agent": "ollie", "source_app_id": "A…", "source_bot_user_id": "U…",
  "target_agent": "riley", "target_bot_user_id": "U…",
  "team_id": "T…", "channel_id": "C…", "room": "orchestrator-team",
  "human_root_ts": "1700000000.000100",
  "requester_session": "ollie-main",
  "body_sha256": "<hex>",
  "posted_ts": ""
}
```

Statuses: `prepared → posting → published | failed | expired`.
Attempts updates under the intent lock. **Recovery is lazy and
Python-owned** (accepted deviation from the Phase 1 barrier-ordering
sentence, which is amended): every company verb invocation first runs a
bounded reconciliation pass over `posting` intents. Reconciliation does
NOT call the Slack API: the delegation post, arriving back through the
switchboard, is itself admitted as a bot-message receipt whose stored
raw event embeds the posted metadata — reconciliation scans the
receipts dir (read-only) for a bot-authored receipt in (team, channel)
whose `metadata.event_payload.nonce` equals the intent nonce; found →
adopt its origin `ts` as `posted_ts`, mark `published`, materialize the
delegation record if absent; not found and past `retry_deadline` → the
intent stays parked (`posting`) — never repost on ambiguity
(`chat.postMessage` is not idempotent). The Go sweep surfaces a count of
stale `posting` intents (age > retry_deadline) on `/healthz` as the
operator signal. Existing-nonce handling at `delegate` time: `prepared`
→ adopt and resume (nothing was ever posted; safe to proceed to
posting); `posting` → resume reconciliation; `published` with its
delegation record still `pending` → the one-pending error below;
anything terminal → mint a fresh nonce at the next `retry_seq`.

`company-delegations/<key>.json` (key per the sanitizer spec) —
materialized before the CLI reports success:

```json
{
  "schema_version": 1,
  "generation": 1,
  "nonce": "gcs-<20hex>",
  "room": "orchestrator-team",
  "team_id": "T…", "channel_id": "C…", "ts": "<posted_ts>",
  "thread_root_ts": "<human_root_ts>",
  "requester_agent": "ollie", "requester_bot_user_id": "U…",
  "requester_session": "ollie-main",
  "expected_responder_agent": "riley",
  "expected_responder_bot_user_id": "U…",
  "created_at": "<RFC3339>", "ttl_seconds": 86400,
  "status": "pending",
  "result_ts": "", "result_claimed_at": ""
}
```

**Result correlation (normative; Slack deviation from Discord).** Slack
has no nested replies — delegation and result share the human root's
thread. A claim requires ALL of: (1) the message passes the five-part
peer trust checklist; (2) it is in thread `thread_root_ts`, authored by
`expected_responder_bot_user_id`; (3) its native mention set includes
`requester_bot_user_id`; (4) its `metadata.event_type ==
"gc_delegation_result"` with `event_payload.nonce == record.nonce` and
`event_payload.delegation_ts == record.ts` (the breadcrumb is
load-bearing for claim ADMISSION — responder chatter, clarifying
questions, and hand-typed posts deliver as ordinary peer input without
consuming the claim; authorship remains the Slack-authoritative trust
anchor, so metadata alone can never claim); (5) route window
`-300s <= now - created_at <= ttl`. `delegate` enforces **at most one
pending delegation per (team, channel, thread_root_ts, responder,
requester)** under the tuple lock; TTL-expired `pending` records count
as not-pending (and are rewritten `expired` under the lock). Go's claim
(`pending → result_claimed`, under the tuple lock, generation-checked)
is replay-idempotent; >1 pending record matching one claim key parks
the receipt fail-closed (`ambiguous_pending_delegations`). A wedged
flow is recoverable without the TTL: `gc slack delegate --cancel --to
<agent>` transitions the caller's own pending record to `expired` under
the lock.

**Pruning (lazy, Python-owned).** Every company verb invocation prunes:
terminal intents and terminal/expired delegation records older than 7
days (retention floor 24h, matching receipts). `retry_seq` counts files
present; pruned files cannot collide because intent creation is O_EXCL
on a fresh nonce.

`company-current-turn/<session>.json` — written atomically by the Go
delivery worker on EVERY company wake, before the gc session POST; the
deterministic context source for the Python verbs (no receipt scanning):

```json
{
  "schema_version": 1,
  "session": "ollie-main",
  "receipt_id": "in-…",
  "team_id": "T…", "channel_id": "C…", "ts": "<origin ts>",
  "room": "orchestrator-team",
  "kind": "ambient | targeted | peer_input | peer_delegation | peer_result",
  "thread_root_ts": "<derived root>",
  "agent": "ollie",
  "delegation_key": "<delegations filename; present iff kind is peer_delegation or peer_result>",
  "delivered_at": "<RFC3339>"
}
```

The delivered reminder text also displays the origin `ts`, and the verbs
accept `--origin-ts` to pin a specific turn when a newer wake has
overwritten the pointer (mismatch without the flag is a hard error
telling the agent to pass it).

Message metadata: delegations post `metadata: {event_type:
"gc_delegation", event_payload: {v: 1, nonce, root_ts, requester,
target}}`; results post `metadata: {event_type: "gc_delegation_result",
event_payload: {v: 1, nonce, delegation_ts}}`. Metadata is embedded in
the switchboard's `message.*` events (no extra scope) and is
workspace-visible and mutable — breadcrumbs plus claim-admission gate;
the durable record stays authoritative. Pilot wire capture must confirm
metadata presence on a real agent-app post event.

Composition contract (both verbs): top-level `text` only — no `blocks`,
no `link_names`, `reply_broadcast` never set, default `parse` (bare URLs
may auto-link; harmless, no notification) — body entity-escaped (`&`,
`<`, `>`); the service-constructed mention is the only live entity.
Delegation text: `<@target_bot_user_id> <escaped body>` with
`thread_ts=human_root_ts`. Result text: `<@requester_bot_user_id>
<escaped body>` with `thread_ts=thread_root_ts`. Synthesis: escaped
body, no live mentions, `thread_ts=thread_root_ts`. All company posts
are therefore visible in the human root's thread (not the channel
timeline). Retry policy on `chat.postMessage`: explicit timeout; 429
honors `Retry-After` within `max_attempts`; definitive 4xx → intent
`failed`; timeout/5xx → reconcile-before-repost.

Identity plumbing: the session namespace is the bound session NAME,
obtained from `GC_SESSION_NAME` (hard error if unset); the anti-spoof
check compares it to the pointer file's `session` and the (room, agent)
binding. `bind-company-agent` rejects binding a session already bound
to a DIFFERENT agent in the same room, so `delegate`'s reverse
resolution session → (room, agent) is unambiguous.

### 2b — Python outbound (`scripts/slack_company_outbound.py` + verbs)

New module `slack_company_outbound.py` (intents store with lock/CAS,
token loader with permission/symlink refusal, sanitizer, escaping/
composition, postMessage with metadata, bounded retries, receipt-based
reconciliation, lazy pruner) + `gc slack delegate` verb (3-file
wrapper, including `--cancel`) + `reply-current` gaining
company-context awareness: it reads `company-current-turn/<session>`;
`kind: peer_delegation` → post the result (acting agent's own token,
metadata gate attached) and report `posted_ts` only on success; `kind:
peer_result` → post the synthesis to the human root with no live
mentions; ambient/targeted → unchanged legacy behavior for non-company
context, company rooms answer into the root thread. Tests: hermetic
(mocked Slack API), covering intent lifecycle incl. receipt-based
reconciliation (nonce receipt found → adopt + materialize; absent →
parked, never reposted), retry_seq nonce freshness after terminal
intents, one-pending-per-tuple under two concurrent delegates (real
flock race test), `--cancel`, composition/escaping (`@channel @here
#general <!channel>` inert; only the target mention live), root
derivation from threaded and unthreaded triggers, token
selection/permission refusal, Retry-After honored, definitive 4xx →
failed, record create-once (EEXIST adopts), pointer-file consumption +
`--origin-ts` mismatch error, pruning.

### 2c — Go ingress (author resolution, trust, correlation, envelopes)

- `company_authors.go`: cached `bot_id → bots.info → user_id` resolver
  (switchboard token, `users:read`), TTL cache + singleflight.
  Outcomes are split: definitive `bot_not_found`/`deleted` → unknown
  bot (terminal for that message); transient (ratelimited — honor
  `Retry-After` — timeout, 5xx, network) → park the receipt
  non-terminally (`author_resolution_pending`) for sweep retry, never
  a terminal `no_delivery`. Corroboration: the event's own
  `app_id`/`bot_profile` fields pre-check, `bots.info`'s `app_id` must
  match the directory agent's `app_id`, `event.user` must match when
  present; any mismatch → unknown bot.
- `company_routing.go`: `CompanyMessage` gains `ResolvedBotUserID
  string` (populated by the delivery worker; keeps ComputeWakeSet
  pure) and `Metadata json.RawMessage`; `slackMessageEvent` gains
  `Metadata json.RawMessage`. The bot-authored mention-wake leg turns
  on: AuthorCompanyBot (resolved, room member) with a native mention of
  an eligible member routes per the table. Reason-code taxonomy
  replacing `company_bot_phase2`: `company_bot_no_mention`,
  `company_bot_not_member`, `unknown_bot`, `company_self` (existing),
  plus routing-stage `mentioned_not_eligible` reasons — Phase 1 tests
  pinning `company_bot_phase2` are updated in the same change.
- `company_peer.go`: five-condition trust checklist with
  machine-readable failure reasons; result-claim per the shared
  contract (tuple lock + generation, metadata gate, requester-mention
  check, replay-idempotent, `ambiguous_pending_delegations` parking);
  plausible in-flight tuple (a `posting` intent exists) → park
  `correlation_pending`; unmatched identifiable replies to the
  switchboard rejected; everything else = ordinary peer delegation
  processing.
- `company_delivery.go`: writes `company-current-turn/<session>.json`
  before each gc POST; envelope stays a single reminder string (gc API
  unchanged) rendered from a pinned template with sections for kind,
  `peer_authority: peer_only`, `root_provenance`, verified human root,
  and the bounded excerpt — every interpolated value (root JSON fields,
  each excerpt line) passes `neutralizeMarkupBoundaries`.
  `TargetDelivery.Kind` grows `peer_delegation`/`peer_result` (Phase 1
  fixtures updated). **Hydration is frozen**: the verified root and the
  excerpt (`conversations.history`/`replies`: max 8 messages, 12KiB
  total, 1024 chars each; failure → `context_status:
  context_unavailable`, never broader routing) are fetched once at
  first delivery and stored in a new receipt field `Hydration
  json.RawMessage`, so redrives re-render identical bytes under the
  same Idempotency-Key. Sweep gains the stale-`posting`-intent count
  for `/healthz` (read-only scan).
- Tests: acceptance 3 (trusted Ollie `@Riley` wakes Riley exactly once
  through the full checklist), 5 (Riley's metadata-gated threaded
  result wakes only Ollie and claims the record; a clarifying question
  without result metadata delivers as peer input and claims nothing;
  replay claims nothing twice), 7 (dormant mentioned agent gets
  current message + verified root + bounded excerpt, no duplicate;
  threaded human trigger derives the parent root), remaining rule-4
  legs (unknown bot, non-member author, self, unresolvable
  `bot_message`, mention of non-member, expired/claimed/ambiguous
  results fall to peer input or park), transient `bots.info` failure
  parks then delivers exactly once, hydration-failure marker, excerpt
  bounds, frozen-hydration byte-identity across redrives,
  pointer-file write ordering.

### 2d — cross-language wire tests

`slack-full/tests/fixtures/company/` golden files: intent record,
delegation record, current-turn pointer, and sanitizer filename
fixtures (hostile/long/dotted components). The Go and Python suites
each parse, validate, and re-derive the same bytes. Interop is
fixture-mediated across the two independent suites:
`fixtures/company/interop/` holds records generated through the REAL
Python code paths (with pinned clocks); the Python suite proves
byte-stable regeneration, and the Go suite claims/consumes those exact
bytes through its real claim and pointer paths. Lock-filename parity is
pinned by identical derivations in both suites (verified:
`dtuple-3a4b34ac4caada68.lock` from both languages for the same key).

Pilot step: capture one real agent-app post event and assert the
mention extractor, author classifier, AND embedded metadata match the
wire shape the contract assumes.

## Phase 3 — synthesis + redrive parity

Sibling synthesis snapshots with group locks and frozen ordering,
`retry-peer-fanout` parity, monotonic replay ordering across restarts,
parked `correlation_pending` references with bounded backoff, visible-ack
reactions (👀/✅/⚠️) config-gated with `already_reacted` treated as
success and silent degradation on reaction caps.

## Phase 4 — per-agent DMs

`message.im` + `im:history` per agent identity app against the shared
events endpoint. Signature verification binds the claimed app: resolve the
app record by `(team_id, api_app_id)` and verify the HMAC against that
record's secret, failing closed on mismatch (trial-HMAC fallback only for
legacy single-app installs). DM admission into the same receipt store with
`(team_id, dm_channel_id, ts)` keys; allowed-human policy; DM-bound
singleton sessions; bot-authored DMs deliver nothing; acceptance rule 12.

## Phase 5 — ledger integration

When the gascity durable-request-ledger lands: swap local admission for
`POST /v0/city/{city}/extmsg/request-receipts` + receipt-lookup recovery,
implement the projector-callback UDS contract and publish-intent outbox
against the ledger API, adopt the cross-repo wire fixture. This is an
explicit state-mapping exercise (`received → spooled`,
`routing → admitting`, terminal states → `terminal`), and the ledger's
receipts never carry raw bodies, so the inner-event payload moves to a
spool-side body store at that point. The Phase 1 origin-key discipline and
ordering are chosen so the swap does not change pack-observable routing
semantics; true end-to-end exactly-once delivery arrives here.
