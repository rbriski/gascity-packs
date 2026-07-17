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
  computes the wake set (1c); resolves each wake through the
  company-bindings snapshot (missing binding → target `failed` with
  reason, no legacy fallback); delivers each target via a new
  `deliverToCompanySession` helper — POST
  `/v0/city/{city}/session/{session}/messages` with header
  `Idempotency-Key: ingress:<id>:target:<session>`, an explicit
  per-request timeout, and the system-reminder-style envelope used by
  alias dispatch (markup-neutralized); marks a target `delivered` only on
  gc's acknowledged 2xx, leaves it `pending` (attempts++) on
  timeout/5xx/ambiguity for sweep retry with the same key; sets the
  terminal receipt status when all targets resolve.
- Parking: a pending receipt whose channel matches no *current* directory
  room (including nil directory) is left pending with
  `Reason: parked_no_directory_room` — never terminally resolved, never
  legacy-delivered; the sweep retries it after every directory change.
- Startup recovery barrier, company-scoped: legacy routes, `/healthz`,
  interactions, and the internal listener serve immediately; company-room
  admissible events get 503 (retryable) until one synchronous
  `Pending()` scan completes and its receipts are enqueued. (Posting
  intents precede pending ingress in the barrier ordering from Phase 2
  on.) `/healthz` detail reports barrier state and the receipt-store
  `WriteFailures()` counter; the gateway status payload includes both plus
  directory/bindings snapshot state and membership warnings.
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
README): bot user, `chat:write`, `app_home.messages_tab_enabled: true`,
`messages_tab_read_only_enabled: false`, interactivity off, **no event
subscriptions** (the DM phase adds `message.im` + `im:history`). Operator
steps documented: create/install per agent, harvest `app_id` +
`bot_user_id` into the directory TOML, register each signing secret with
the adapter (existing `import-app` flow), drop each bot token into
`secrets/bot-token-<agent>.txt` (0600/0700), invite each member bot to its
rooms. Switchboard manifest gains `channels:read`, `groups:read`
(membership checks, Phase 1) and `users:read` (`bots.info` author
resolution, Phase 2) — scope changes require reinstall, called out in the
runbook.

## Phase 2 — identities, delegation, results, hydration

- Token selection by directory agent name; delegation/result posts use the
  acting agent's token (real identity); `chat:write.customize` untouched
  for legacy rooms.
- Durable posting intents before any `chat.postMessage`
  (`prepared → posting → published`, CAS attempts, bounded retries
  honoring `Retry-After`, explicit HTTP timeout), content-addressed nonce
  in `metadata.event_payload`; crash reconciliation via
  `conversations.replies` **with `include_all_metadata=true`**;
  reconciliation that cannot find the nonce parks the intent (fail-closed
  test: absent metadata → park, never repost).
- Composition contract enforced and tested: company posts are top-level
  `text` only — no `blocks`, no `link_names`, default `parse` — with
  entity-escaped bodies; test that `@channel @here #general <!channel>`
  in a body produces no live notification entities.
- Author resolution: cached `bot_id` → `bots.info` → `user_id` mapping
  against the directory (requires `users:read`); `event.user`
  corroboration when present; ambiguity fails closed. Turns on the
  bot-mention wake leg in `ComputeWakeSet` (AuthorCompanyBot resolution).
- `gc slack delegate` / `reply-current` result semantics; delegation
  records keyed by posted `(channel, ts)` + expected responder; peer trust
  checklist; `slack_peer_delegation` / `slack_peer_result` envelopes with
  `peer_authority` / `root_provenance`; bounded room-excerpt hydration.
- Pilot step: capture one real agent-app post event and assert the mention
  extractor and author classifier match its wire shape.

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
