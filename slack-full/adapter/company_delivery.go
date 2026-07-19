package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// companyGateway owns the Slack company-rooms durable-admission and
// delivery path (Slack company-rooms Phase 1d). It sits between the
// signature-verified /slack/events handler and the legacy ack-first path:
// an event that targets an imported company room is admitted to the
// durable receipt store before the HTTP acknowledgment, then routed and
// delivered asynchronously; every other event falls through to the legacy
// path byte-for-byte.
//
// A nil *companyGateway is a valid "company path disabled" value — every
// method guards its nil receiver — so handlers can call through cfg
// unconditionally.
type companyGateway struct {
	// cfg is captured by value at construction (after cfg.dispatchSem is
	// initialized). The gateway never reads cfg.companyGateway, so the
	// copy having a nil back-pointer is fine; the dispatch semaphore is a
	// channel and therefore shared with the live cfg.
	cfg config

	dirStore  *companyDirectoryStore
	bindStore *companyBindingsStore
	// dmBindStore / agentApps are the Phase 4 per-agent DM registries. Both
	// share the never-fatal load contract of dirStore/bindStore (nil snapshot
	// = feature dark). agentApps binds a delivering app's api_app_id to its
	// signing secret; dmBindStore maps an agent to its singleton DM session.
	dmBindStore *dmBindingsStore
	agentApps   *agentAppsStore

	// receipts is the durable ingress store. It may be nil at startup
	// (degraded mode) when NewIngressReceiptStore failed; startRecovery
	// keeps retrying construction from ingressDir and promotes to normal
	// operation (opening the barrier) once it succeeds. Held as an atomic
	// pointer because delivery/sweep/healthz goroutines read it lock-free.
	receipts   atomic.Pointer[IngressReceiptStore]
	ingressDir string
	// storeErr holds the last store-construction error message while the
	// store is unavailable, surfaced on /healthz as the degraded paging
	// hook. Nil once the store is live.
	storeErr atomic.Pointer[string]
	// storeMu serializes store-construction attempts (startRecovery).
	storeMu sync.Mutex

	// deliveryFailures counts failed target delivery attempts (retryable
	// pending, definitive 4xx, and attempts-exhausted), surfaced on
	// /healthz so silent delivery loss is observable.
	deliveryFailures atomic.Uint64

	deliverClient *http.Client

	// slackToken is the switchboard bot token used for bots.info author
	// resolution and conversations.* hydration. Empty in Phase 1 / tests
	// that do not exercise the bot legs.
	slackToken string
	// slackClient is the HTTP client for Slack API calls (author resolution,
	// hydration). Separate from deliverClient (which targets gc).
	slackClient *http.Client
	// authors resolves bot_id -> registered directory agent (Phase 2c).
	// Swappable in tests; the production value is an HTTP botInfoResolver.
	authors companyAuthorResolver
	// hydrate fetches the frozen context bundle for one message. Swappable in
	// tests; the production value calls Slack conversations.* with the
	// switchboard token (room deliveries only).
	hydrate func(msg CompanyMessage) companyHydration
	// hydrateDM fetches the frozen context bundle for a DM using the OWNER
	// agent's bot token (im:history) — the switchboard token must never touch
	// a DM channel. Swappable in tests; the production value calls Slack
	// conversations.* with the passed owner token. Reused for mpim hydration
	// (mpim:history), where the token is selected by the admission-owner-first
	// fallback (spec §Hydration).
	hydrateDM func(token string, msg CompanyMessage) companyHydration
	// mpimMemberProbe probes an mpim's membership with a WOKEN agent's own token
	// (conversations.info) before that agent's first delivery, so a forged event
	// for a group the agent is not in fails the target rather than waking it
	// (spec §Admission membership probe). Swappable in tests; nil is treated as
	// an advisory proceed (availability floor, like a probe network error).
	mpimMemberProbe func(token, channel string) mpimProbeOutcome

	// Phase 2 shared-state directories the ingress side reads/writes.
	intentsDir     string
	delegationsDir string
	turnsDir       string
	locksDir       string
	// stalePostingIntents is the read-only sweep count of intents stuck past
	// their retry_deadline, surfaced on /healthz as the operator signal.
	stalePostingIntents atomic.Int64

	// barrier gates admission: company-admissible events receive 503
	// (retryable) until one synchronous Pending() scan completes at
	// startup and its receipts are enqueued.
	barrier atomic.Bool

	// mu guards inflight, the in-process single-flight claim set keyed by
	// receipt id. Cross-process safety rests on the receipt store's
	// generation counter; this prevents two goroutines in THIS process
	// from delivering the same receipt concurrently.
	mu       sync.Mutex
	inflight map[string]bool

	// deliverWG tracks in-flight delivery goroutines so tests can wait
	// for delivery to settle. It is Add'd synchronously inside
	// triggerDelivery before the goroutine starts.
	deliverWG sync.WaitGroup

	// chains is the per-root in-process ownership registry (S5): one active
	// chain per root triple, so a sweep pass or a live result trigger for an
	// owned root enqueues into the running chain instead of racing it.
	chains *chainRegistry

	// companyCorrelationParked is the sweep-computed count of receipts parked
	// correlation_pending or ambiguous_pending_delegations, surfaced on
	// /healthz (S7/D5 operator signal).
	companyCorrelationParked atomic.Int64

	// visibleAcks gates the config-driven visible-ack reactions
	// (SLACK_COMPANY_VISIBLE_ACKS). Off by default; all ack traffic is
	// best-effort, asynchronous inside the delivery worker, and never changes
	// receipt status or counts as a delivery failure.
	visibleAcks bool

	// reactHook performs one visible-ack reaction (add/remove) over the
	// switchboard token and classifies it into the ack taxonomy; replyHook
	// posts the one threaded failure notice. Both are function fields so tests
	// can observe ack traffic without a live Slack; production wraps slackReact
	// and the chat.postMessage path (the single reactions/message POST paths).
	reactHook func(method, channel, ts, name string) ackOutcome
	replyHook func(channel, threadTS, text string) bool
	// reactHookTok / replyHookTok are the token-parameterized ack hooks used
	// by DM receipts (the ack actor is the owner agent's token, not the
	// switchboard). When set they take precedence over reactHook/replyHook, so
	// production always routes through the token-aware path (rooms pass the
	// switchboard token; DMs pass the owner token). Tests that only exercise
	// rooms keep setting the untyped reactHook/replyHook spies.
	reactHookTok func(token, method, channel, ts, name string) ackOutcome
	replyHookTok func(token, channel, threadTS, text string) bool

	// secretsDir is the company secrets dir (SLACK_COMPANY_SECRETS_DIR),
	// shared with the Python side, from which DM owner bot tokens are loaded.
	secretsDir string
	// verifySessions gates the advisory session-existence guard (Phase 4).
	verifySessions bool
	// sessionCache is the positive-only session-existence cache: a (city,
	// session) that GET-verified 200 is cached for sessionCacheTTL. Negatives
	// are never cached (sessions 404-then-materialize; aliases 409 transiently).
	sessionCacheMu sync.Mutex
	sessionCache   map[string]time.Time

	// Self-heal counters (P0 delivery hardening), surfaced on /healthz.
	// targetReresolved counts frozen targets whose stale session was rewritten
	// to the current binding on a session-not-found retry (company_target_reresolved);
	// materializeRequests counts session-materialization POSTs fired for cold
	// bound sessions (company_materialize_requests).
	targetReresolved    atomic.Uint64
	materializeRequests atomic.Uint64
	// materializeAttempts throttles auto-materialization to at most ONE POST per
	// (city, session) per sweep interval, keyed by city+"\x00"+session → the last
	// attempt time. The delivery worker and the sweep both fire through this gate.
	materializeMu       sync.Mutex
	materializeAttempts map[string]time.Time

	// dmSigRejects counts app-bound signature rejections (a DM event whose
	// api_app_id does not match the secret it was signed with) — /healthz
	// company_dm_sig_reject. dmTokenMissing counts DM deliveries that fell back
	// to context_unavailable + degraded acks because the owner token was
	// missing — /healthz company_dm_token_missing.
	dmSigRejects   atomic.Uint64
	dmTokenMissing atomic.Uint64
	// dmStatusCounts is the sweep-computed gauge of dm-family receipts by kind
	// and status, surfaced on /healthz (company_dm_receipts + company_mpim_receipts).
	// Stored as an immutable pointer swapped each sweep pass.
	dmStatusCounts atomic.Pointer[dmFamilyReceiptCounts]
	// bodyIntegrity is the sweep-computed body gauge surfaced on /healthz
	// (company_body_missing + company_bodies_redacted). Swapped each sweep pass
	// from the single SweepAndPending scan (Phase 5 body-store split).
	bodyIntegrity atomic.Pointer[bodyIntegrityCounts]

	retention     time.Duration
	sweepInterval time.Duration
	staleWindow   time.Duration
	now           func() time.Time
}

// sessionCacheTTL bounds a positive session-existence cache entry (Phase 4).
const sessionCacheTTL = 10 * time.Minute

// Advisory session-guard target details (Phase 4). Left on a target that the
// guard blocked; preserved through the attempt-cap exhaustion path so a
// company-redrive shows why the target never posted.
const (
	companyDetailSessionMissing   = "session_missing"
	companyDetailSessionAmbiguous = "session_ambiguous"
	// companyDetailMaterializing marks a bound target whose cold session a
	// materialization POST has just been fired for (P0 self-heal). It is a
	// recoverable, guard-held pending state exactly like session_missing — the
	// sweep re-probes on the 60s cadence and the user-visible failure notice is
	// suppressed while the attempts budget remains.
	companyDetailMaterializing = "materializing"
	// companyReasonFailedDMUnbound is the definitive detail on a DM target
	// whose owner agent has no dm_bindings row — recoverable via company-redrive
	// after a binding is imported (the rooms unbound rule, not a park).
	companyReasonFailedDMUnbound = "failed_dm_unbound"
)

// dmReceiptStatusCounts is the per-status receipt gauge for one dm-family kind.
type dmReceiptStatusCounts struct {
	Received   int
	Routing    int
	Delivered  int
	NoDelivery int
	Failed     int
}

// dmFamilyReceiptCounts splits the dm-family /healthz gauge by receipt kind: the
// DM breakdown surfaces as company_dm_receipts_*, the group-DM breakdown as
// company_mpim_receipts_* (spec §Kind-dispatch inventory: dm-family folded into
// the single scan, reported as company_dm_receipts plus a company_mpim_receipts
// breakdown).
type dmFamilyReceiptCounts struct {
	DM   dmReceiptStatusCounts
	Mpim dmReceiptStatusCounts
}

// Company delivery tunables. Retention is Discord parity (7 days); the
// receipt store enforces its own 24h floor. The stale-reclaim window
// keeps the sweep from stealing a claim from a still-running worker.
const (
	companyDeliverTimeout        = 15 * time.Second
	companyReceiptRetention      = 7 * 24 * time.Hour
	companySweepInterval         = 60 * time.Second
	companyStaleReclaimWindow    = 5 * time.Minute
	companyRecoveryRetryInterval = 5 * time.Second
	companyReasonParked          = "parked_no_directory_room"
	// companyReasonBodyIntegrity parks a live receipt whose body sidecar is missing
	// or digest-mismatched (C5/m8): routing an empty message from a null body would
	// terminalize it under an unrelated reason (no_delivery / dm_author_not_allowed)
	// and destroy the wake. Parking keeps recovery open — a later orphan-adoption
	// repair or operator action can restore the body and the sweep redelivers.
	// Plain every-sweep cadence (not an S7 recovery reason, never budget-consuming).
	companyReasonBodyIntegrity   = "parked_body_integrity"
	companyDeliverRequestTag     = "gc-slack-adapter-company"
	companyMaxErrorBodyBytesRead = 4096
	companyTargetPending         = "pending"
	companyTargetDelivered       = "delivered"
	companyTargetFailed          = "failed"
	// companyMaxDeliveryAttempts bounds per-target redrive: once a target
	// has been attempted this many times without success it is marked
	// failed with reason "attempts_exhausted", so a permanently-unroutable
	// target cannot keep a receipt non-terminal (and immortal) forever.
	companyMaxDeliveryAttempts     = 12
	companyReasonAttemptsExhausted = "attempts_exhausted"
	// Target map key prefixes. Bound and unbound targets live under
	// disjoint namespaces (first byte '!' vs 's') separated from the value
	// by NUL, so a session literally named "<agent>" — or even
	// "!unbound:<agent>" — can never collide with a failed-unbound record's
	// key. The Idempotency-Key header still derives from the raw session.
	companyBoundTargetKeyPrefix   = "s\x00"
	companyUnboundTargetKeyPrefix = "!unbound\x00"
)

// companyHealthStatus is the process-singleton gateway pointer read by
// handleHealthz to append the company barrier / write-failure detail
// lines. Set once in main() after the gateway is wired; nil in every test
// that does not exercise the production main(), so /healthz keeps its
// two-line shape there.
var companyHealthStatus atomic.Pointer[companyGateway]

// newCompanyGateway builds a gateway over the loaded stores. cfg must
// already carry the initialized dispatch semaphore. receipts may be nil:
// the gateway then starts degraded (barrier stays closed, company events
// get 503, never legacy) and startRecovery retries construction from
// cfg.companyIngressDir until it succeeds.
func newCompanyGateway(cfg config, dir *companyDirectoryStore, bind *companyBindingsStore, receipts *IngressReceiptStore) *companyGateway {
	slackClient := &http.Client{Timeout: companyDeliverTimeout}
	g := &companyGateway{
		cfg:                 cfg,
		dirStore:            dir,
		bindStore:           bind,
		ingressDir:          cfg.companyIngressDir,
		deliverClient:       &http.Client{Timeout: companyDeliverTimeout},
		slackToken:          cfg.slackBotToken,
		slackClient:         slackClient,
		authors:             newBotInfoResolver(cfg.slackBotToken),
		intentsDir:          cfg.companyIntentsDir,
		delegationsDir:      cfg.companyDelegationsDir,
		turnsDir:            cfg.companyTurnsDir,
		locksDir:            cfg.companyLocksDir,
		inflight:            make(map[string]bool),
		chains:              newChainRegistry(),
		visibleAcks:         cfg.companyVisibleAcks,
		secretsDir:          cfg.companySecretsDir,
		verifySessions:      cfg.companyVerifySessions,
		sessionCache:        make(map[string]time.Time),
		materializeAttempts: make(map[string]time.Time),
		retention:           companyReceiptRetention,
		sweepInterval:       companySweepInterval,
		staleWindow:         companyStaleReclaimWindow,
		now:                 time.Now,
	}
	// Phase 4 DM registries: loaded here (never-fatal) against the directory
	// snapshot so a DM binding / agent-app join can be validated at load. An
	// absent file installs a nil snapshot (feature dark), matching the two
	// Phase 1 registries.
	g.dmBindStore = &dmBindingsStore{}
	_ = g.dmBindStore.Load(cfg.companyDMBindingsPath, dir.Snapshot())
	g.agentApps = &agentAppsStore{}
	_ = g.agentApps.Load(cfg.companyAgentAppsPath, dir.Snapshot())
	g.hydrate = func(msg CompanyMessage) companyHydration {
		return fetchCompanyHydration(g.slackToken, g.slackClient, msg)
	}
	g.hydrateDM = func(token string, msg CompanyMessage) companyHydration {
		return fetchCompanyHydration(token, g.slackClient, msg)
	}
	g.mpimMemberProbe = func(token, channel string) mpimProbeOutcome {
		return probeMpimMembership(token, g.slackClient, channel)
	}
	// Visible-ack hooks (token-parameterized): the ack actor's token is chosen
	// per receipt — the switchboard token owns reactions:write and the room
	// receipt lifecycle; a DM receipt's actor is the owner agent's token. Both
	// reuse the single existing POST paths (no second reactions/message
	// implementation) over the gateway's timeout-bounded client so a hung Slack
	// call cannot wedge the delivery worker. The untyped reactHook/replyHook
	// fields are left nil in production; only room-only test spies set them.
	g.reactHookTok = func(token, method, channel, ts, name string) ackOutcome {
		return slackReact(g.slackClient, token, method, channel, ts, name)
	}
	g.replyHookTok = func(token, channel, threadTS, text string) bool {
		if _, err := postMessageWithClient(g.slackClient, token, slackPostMessageReq{
			Channel:  channel,
			ThreadTS: threadTS,
			Text:     text,
		}); err != nil {
			log.Printf("company: visible-ack failure reply channel=%s thread=%s: %v", channel, threadTS, err)
			return false
		}
		return true
	}
	if receipts != nil {
		g.receipts.Store(receipts)
		g.ingressDir = receipts.dir
	}
	return g
}

// agentAppsSnapshot returns the current agent-apps snapshot, nil-safe on a nil
// gateway / nil store so the HTTP verification path can call through cfg
// unconditionally (a nil *AgentApps answers every query fail-closed).
func (g *companyGateway) agentAppsSnapshot() *AgentApps {
	if g == nil || g.agentApps == nil {
		return nil
	}
	return g.agentApps.Snapshot()
}

// recordDMSigReject increments the app-bound signature rejection counter
// (/healthz company_dm_sig_reject). Nil-safe.
func (g *companyGateway) recordDMSigReject() {
	if g == nil {
		return
	}
	g.dmSigRejects.Add(1)
}

// peerEnv bundles the Phase 2 shared-state directories plus the gateway clock
// for the correlation/claim layer.
func (g *companyGateway) peerEnv() companyPeerEnv {
	return companyPeerEnv{
		delegationsDir: g.delegationsDir,
		intentsDir:     g.intentsDir,
		locksDir:       g.locksDir,
		retention:      g.retention,
		now:            g.now,
	}
}

// store returns the live receipt store, or nil while the gateway is
// degraded (store construction has not yet succeeded).
func (g *companyGateway) store() *IngressReceiptStore {
	if g == nil {
		return nil
	}
	return g.receipts.Load()
}

// ensureStore returns the live receipt store, constructing it on first
// success when the gateway started degraded. Construction is serialized;
// a failure records the error for /healthz and leaves the gateway degraded.
func (g *companyGateway) ensureStore() (*IngressReceiptStore, error) {
	if s := g.receipts.Load(); s != nil {
		return s, nil
	}
	g.storeMu.Lock()
	defer g.storeMu.Unlock()
	if s := g.receipts.Load(); s != nil {
		return s, nil
	}
	s, err := NewIngressReceiptStore(g.ingressDir)
	if err != nil {
		msg := err.Error()
		g.storeErr.Store(&msg)
		return nil, err
	}
	g.receipts.Store(s)
	g.storeErr.Store(nil)
	return s, nil
}

// setStoreError records an initial store-construction failure so /healthz
// reports the degraded state immediately, before startRecovery's first
// retry. Nil-safe.
func (g *companyGateway) setStoreError(err error) {
	if g == nil || err == nil {
		return
	}
	msg := err.Error()
	g.storeErr.Store(&msg)
}

// tryHandleEvent applies the company-room admission gate. It returns true
// (having written the HTTP response) when it owns the event, and false to
// let the caller fall through to the legacy path byte-for-byte. A nil
// gateway always returns false. agentApps is the caller's once-per-request
// registration snapshot, forwarded to the DM gate so admission uses exactly the
// snapshot the HMAC verification used (m7); a nil snapshot answers fail-closed.
func (g *companyGateway) tryHandleEvent(w http.ResponseWriter, r *http.Request, env slackEventEnvelope, agentApps *AgentApps) bool {
	if g == nil {
		return false
	}
	if env.Type != "event_callback" || len(env.Event) == 0 {
		return false
	}
	var ev slackMessageEvent
	if err := json.Unmarshal(env.Event, &ev); err != nil {
		// Undecodable inner event: let the legacy async path log + drop it
		// exactly as before rather than 5xx an unkeyable body.
		return false
	}
	switch ev.Type {
	case "message":
		// Fall through to company admission below.
	case "app_mention":
		// Company rooms own app_mention: the paired message.channels event
		// is the canonical admitted copy, so the mention twin is acked with
		// 200 and creates no receipt — it must never reach legacy dispatch,
		// which would double-deliver by waking the channel-bound session.
		// Non-company channels keep today's app_mention behavior byte-for-byte.
		if _, ok := g.dirStore.Snapshot().RoomByChannel(env.TeamID, ev.Channel); !ok {
			return false
		}
		w.WriteHeader(http.StatusOK)
		return true
	default:
		// Other non-message types follow today's path byte-for-byte.
		return false
	}
	// DM admission (Phase 4): a message.im from a registered agent app is a
	// per-agent DM owned by the DM gateway. Any other channel_type falls
	// through to the room/legacy paths; an im from an app that is NOT a
	// registered agent app (a DM to the switchboard) also falls through so its
	// existing legacy behavior is byte-for-byte preserved.
	if ev.ChannelType == "im" {
		return g.tryHandleDMEvent(w, r, env, ev, agentApps)
	}
	// Group-DM admission (Phase 4b): a message.mpim owned by the mpim gateway.
	// A registered agent app's copy admits (origin-key dedup absorbs the other
	// member apps' copies); a switchboard-signed mpim — the switchboard also
	// subscribes to message.mpim — is acked 200 with NO receipt and NO legacy
	// dispatch (it has no business in agent group DMs). Unlike a switchboard DM,
	// an unowned mpim must NOT fall through: the room path would N-plicate it.
	if ev.ChannelType == "mpim" {
		return g.tryHandleMpimEvent(w, r, env, ev, agentApps)
	}
	if _, ok := g.dirStore.Snapshot().RoomByChannel(env.TeamID, ev.Channel); !ok {
		// Not an imported company room (including the nil-directory case):
		// the legacy path handles it. Parking applies only to receipts
		// already admitted, never to admission itself.
		return false
	}

	// From here the gateway owns the HTTP response.

	// Admissibility gate: an explicit subtype allowlist. A non-admissible
	// subtype (channel_join, topic change, hidden edit/delete, …) is
	// acked 200 and creates no receipt.
	if !AdmissibleSubtype(ev.Subtype) {
		w.WriteHeader(http.StatusOK)
		return true
	}
	// An event with no stable (team, channel, ts) identity is logged and
	// dropped with 200 — never 5xx, which would burn Slack's retry budget
	// on an unkeyable event.
	if env.TeamID == "" || ev.Channel == "" || ev.TS == "" {
		log.Printf("company: dropping unkeyable event team=%q chan=%q ts=%q", clipTeamIDForLog(env.TeamID), ev.Channel, ev.TS)
		w.WriteHeader(http.StatusOK)
		return true
	}
	// Startup recovery barrier: company-admissible events are retryable
	// (503, no x-slack-no-retry) until the first Pending() scan completes.
	// The barrier also stays closed while the gateway is degraded (store
	// construction has not yet succeeded), so a degraded store yields 503
	// here, never a legacy fallthrough.
	if !g.barrier.Load() {
		w.WriteHeader(http.StatusServiceUnavailable)
		return true
	}
	// Defensive: the barrier only opens after the store is live, but never
	// admit against a nil store — 503 (retryable), never legacy.
	store := g.store()
	if store == nil {
		w.WriteHeader(http.StatusServiceUnavailable)
		return true
	}

	origin := ReceiptOrigin{TeamID: env.TeamID, ChannelID: ev.Channel, TS: ev.TS}
	retryNum, retryReason := parseSlackRetryHeaders(r.Header)
	receipt := &IngressReceipt{
		Origin:      origin,
		EventID:     env.EventID,
		APIAppID:    env.APIAppID,
		RetryNum:    retryNum,
		RetryReason: retryReason,
		Status:      ingressStatusReceived,
		// Freeze the human root at admission (thread_ts, else origin ts) so every
		// root-keyed derivation survives a later body redaction/loss (C7).
		ThreadRootTS: deriveHumanRootTS(decodeCompanyMessage(origin, env.Event)),
		Event:        append(json.RawMessage(nil), env.Event...),
	}
	created, _, err := store.Admit(receipt)
	if err != nil {
		// Receipt-store write failure. 503 WITHOUT x-slack-no-retry so
		// Slack redelivers (~immediately, +1m, +5m, and hourly for 24h
		// with Delayed Events). WriteFailures() already incremented
		// inside Admit; it surfaces on /healthz as the paging hook.
		log.Printf("company: admit failed origin=%+v: %v", origin, err)
		w.WriteHeader(http.StatusServiceUnavailable)
		return true
	}
	if !created {
		// Duplicate origin — an x-slack-retry redelivery of an already
		// admitted event terminates here: ack, no second delivery.
		w.WriteHeader(http.StatusOK)
		return true
	}
	// Admitted. Ack the transport, then trigger asynchronous delivery. If
	// no dispatch slot is free the receipt stays pending and the sweep
	// recovers it — backpressure, never a silent drop.
	w.WriteHeader(http.StatusOK)
	g.triggerDelivery(origin)
	return true
}

// parseSlackRetryHeaders extracts Slack's redelivery hints. A missing or
// non-numeric X-Slack-Retry-Num is treated as 0 (first delivery).
func parseSlackRetryHeaders(h http.Header) (int, string) {
	num := 0
	if v := strings.TrimSpace(h.Get("X-Slack-Retry-Num")); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			num = n
		}
	}
	return num, h.Get("X-Slack-Retry-Reason")
}

// triggerDelivery acquires a dispatch slot and fans the delivery of one
// receipt out to a goroutine. On saturation it logs and returns without
// delivering; the receipt stays pending for the sweep. The dispatch
// semaphore, shared with the legacy path, is the process-wide backpressure
// bound. deliverWG.Add happens synchronously so tests can Wait reliably.
func (g *companyGateway) triggerDelivery(origin ReceiptOrigin) {
	if g == nil {
		return
	}
	// S5/S6: a live result-bearing trigger for a root with an active replay
	// chain is routed into that chain instead of racing it (the chain owner
	// will deliver it in order). Cheap when no chain is active.
	if g.enqueueForRoot(origin) {
		return
	}
	// Use the non-counting acquire: a company receipt that finds no slot
	// stays durably pending for the sweep (backpressure), which is NOT a
	// dropped delivery. Counting it would pollute dispatch_dropped_total and
	// the legacy drop-summary heartbeat, masking real legacy loss (F10).
	release, _, ok := g.cfg.tryAcquireDispatchSlot()
	if !ok {
		log.Printf("company: dispatch slot unavailable; receipt %s left pending for sweep", receiptID(origin))
		return
	}
	g.deliverWG.Add(1)
	go func() {
		defer g.deliverWG.Done()
		defer release()
		g.deliverReceipt(origin)
	}()
}

// deliverReceipt is the async delivery worker for one receipt. It holds an
// in-process single-flight claim for the receipt id, claims the receipt by
// a generation-checked Update to "routing", computes the wake set from the
// current directory snapshot, resolves each wake through the bindings
// snapshot, delivers to each bound session, and records the terminal
// status once every target settles. A receipt whose channel matches no
// room in the current snapshot is parked (never terminally resolved,
// never legacy-delivered).
func (g *companyGateway) deliverReceipt(origin ReceiptOrigin) deliverOutcome {
	if g == nil {
		return deliverError
	}
	id := receiptID(origin)
	if !g.acquireSingleFlight(id) {
		return deliverBusy
	}
	defer g.releaseSingleFlight(id)

	store := g.store()
	if store == nil {
		return deliverError // degraded: no store to deliver against yet
	}
	r, err := store.Get(origin)
	if err != nil {
		log.Printf("company: delivery read receipt %s: %v", id, err)
		return deliverError
	}
	if r == nil || isTerminalStatus(r.Status) {
		return deliverTerminal // already settled — safe to advance the chain
	}

	// Body-integrity gate (C5/m8): resolve the body through loadBody (which
	// classifies) rather than the degrade-to-null accessor. A live receipt whose
	// sidecar is missing or digest-mismatched is a recoverable INTEGRITY error, not
	// a routing outcome — routing an empty message from null would terminalize it
	// under an unrelated reason and destroy the wake. Park it (non-terminal,
	// sweep-retried, counted); a later orphan-adoption repair or operator restore
	// reopens delivery. A redacted body is deliberate and keeps the degrade path
	// (redaction is guarded to terminal receipts, so a non-terminal receipt never
	// reaches delivery redacted); an embedded/ok body proceeds.
	body, bodyStat := store.loadBody(r)
	if bodyStat == bodyMissing || bodyStat == bodyMismatch {
		g.parkWithReason(r, companyReasonBodyIntegrity)
		return deliverParkedPreclaim
	}
	msg := decodeCompanyMessage(origin, body)

	// DM-family branch (Phase 4 / 4b): a per-agent DM or group DM has its own
	// routing (owner-app join, self-echo/bot-author, allowed-human policy, and —
	// for mpim — the mention set + membership probe) and its own owner-token
	// custody. Neither takes the result-serialization lock (a direct message is
	// never result-bearing). An mpim receipt routed down the room path would park
	// forever on RoomByChannel (spec §Kind-dispatch inventory), so the family
	// gate is checked BEFORE the room resolution below.
	if isDMFamilyKind(r.Kind) {
		if r.Kind == receiptKindMpim {
			return g.deliverMpimReceipt(r, origin, msg)
		}
		return g.deliverDMReceipt(r, origin, msg)
	}

	// S6 live ordering: hold dgser (root serialization lock) across the whole
	// result-bearing path — from before correlation through finalize — so the
	// snapshot order at the requester equals delivery order. Gated on the
	// message classification (bot-authored AND gc_delegation_result metadata,
	// the only messages that can claim); dgser is the highest-rank lock, so it
	// is always acquired before dgroup/dtuple (deadlock freedom).
	if isResultBearing(msg) {
		lock, lerr := acquireCompanyLock(g.locksDir, rootSerialLockName(origin.TeamID, origin.ChannelID, receiptRootTS(r, msg)))
		if lerr != nil {
			log.Printf("company: dgser acquire receipt=%s: %v", id, lerr)
			return deliverError
		}
		defer lock.release()
	}

	// Visible-ack admission hook (config-gated, best-effort): the first
	// delivery attempt puts 👀 on the origin message. Never blocks or fails
	// delivery, never changes receipt status.
	g.applyAdmissionAck(r)

	dir := g.dirStore.Snapshot()
	room, ok := dir.RoomByChannel(origin.TeamID, origin.ChannelID)
	if !ok {
		// Channel matches no room in the CURRENT snapshot (directory
		// removed, shrunk, or failed to load) — park it.
		g.parkReceipt(r)
		return deliverParkedPreclaim
	}

	// Frozen route (design step 9 / plan 1d): the wake set is computed ONCE,
	// at first delivery. When the receipt already carries recorded targets a
	// redrive drives THOSE targets to terminal states and never recomputes —
	// so a directory that shrinks between redrives can never silently drop a
	// recorded pending target. Terminal no_delivery is legal only when there
	// are no recorded targets AND the freshly computed wake set is empty.
	if len(r.Targets) == 0 {
		// Author resolution (Phase 2c): a bot author is resolved through
		// bots.info before routing so ComputeWakeSet sees the authoritative
		// bot user id. A transient failure parks the receipt for the sweep;
		// an unknown bot falls through the classifier to unknown_bot.
		var authorAgent *CompanyAgent
		if isBotAuthored(msg) {
			res := resolveCompanyAuthor(g.authors, dir, msg)
			switch res.Outcome {
			case botResolveTransient:
				g.parkWithReason(r, peerParkResolutionPending)
				return deliverParkedPreclaim
			case botResolveOK:
				msg.ResolvedBotUserID = res.Agent.BotUserID
				authorAgent = res.Agent
			}
			// botResolveUnknown: leave ResolvedBotUserID empty.
		}

		decision := ComputeWakeSet(dir, msg, g.cfg.companySelfBotUserID)
		if decision.Room == nil {
			g.parkReceipt(r)
			return deliverParkedPreclaim
		}
		if len(decision.Wakes) == 0 {
			// Admitted but nobody woken (bot author, empty ambient set,
			// mentioned-but-ineligible, …): terminal no_delivery carrying the
			// machine-readable routing reason.
			if err := g.commitReceipt(r, func(cur *IngressReceipt) {
				cur.Status = ingressStatusNoDelivery
				cur.Reason = decision.Reason
			}); err != nil {
				log.Printf("company: finalize no_delivery %s: %v", id, err)
				return deliverError
			}
			g.applyTerminalAck(r)
			return deliverTerminal
		}

		// Freeze the wake set into (agent, kind, delegation_key) triples. For
		// a company-bot author the peer layer refines each wake into a
		// delegation vs a metadata-gated result (claiming the record), and may
		// park the receipt fail-closed on an ambiguous or in-flight
		// correlation.
		frozen, park := g.freezeWakes(dir, room, msg, decision, authorAgent)
		if park != "" {
			g.parkWithReason(r, park)
			return g.parkAdvanceOutcome(r)
		}

		// Freeze the synthesis snapshot for the peer_result leg (at most one per
		// receipt) into the SAME routing commit that freezes targets, so a
		// redrive re-renders byte-identical synthesis fields even if the record
		// is later pruned — the same frozen-bytes discipline as Hydration.
		var synthesis json.RawMessage
		for _, fw := range frozen {
			if fw.Kind != wakeKindPeerResult || fw.Snapshot == nil {
				continue
			}
			data, merr := json.Marshal(fw.Snapshot)
			if merr != nil {
				log.Printf("company: marshal synthesis snapshot %s: %v", id, merr)
				return deliverError
			}
			synthesis = data
			break
		}

		// Claim: mark routing and record every target — with its idempotency
		// key — BEFORE any gc submission, so a crash mid-delivery replays
		// with the same key. A missing binding resolves to a failed target
		// with no legacy fallback. This freezes the route.
		bindings := g.bindStore.Snapshot()
		now := g.now().UTC()
		if err := g.commitReceipt(r, func(cur *IngressReceipt) {
			cur.Status = ingressStatusRouting
			cur.Reason = ""
			if len(synthesis) > 0 {
				cur.Synthesis = synthesis
			}
			// A redrive that finds the record materialized (Python's lazy
			// reconciliation ran) claims and delivers normally: clear the S7
			// recovery backoff fields so a prior correlation park leaves no trace.
			cur.RecoveryAttempts = 0
			cur.RecoveryNextAt = time.Time{}
			cur.RecoveryReason = ""
			g.ensureTargets(cur, room, frozen, bindings, now)
		}); err != nil {
			log.Printf("company: claim routing %s: %v", id, err)
			return deliverError
		}
	}

	// Frozen hydration (Phase 2c): fetch the verified root + bounded excerpt
	// exactly once, at first delivery, and persist it so redrives re-render
	// byte-identical reminders. Only fetched when at least one bound target is
	// still pending (nobody to hydrate otherwise).
	//
	// A persist failure must NOT deliver with unpersisted hydration: commitReceipt
	// sets r.Hydration in memory before the Update, so proceeding would POST bytes
	// that are not on disk and a later redrive would refetch a possibly-different
	// bundle and re-render a divergent body under the same Idempotency-Key. Leave
	// the target pending (frozen-byte identity preserved) so the sweep refetches
	// and re-persists before any POST.
	if len(r.Hydration) == 0 && hasPendingBoundTarget(r) {
		hy := g.hydrate(msg)
		data, merr := json.Marshal(hy)
		if merr != nil {
			log.Printf("company: marshal hydration %s: %v", id, merr)
			return deliverError
		}
		if err := g.commitReceipt(r, func(cur *IngressReceipt) {
			cur.Hydration = data
		}); err != nil {
			log.Printf("company: freeze hydration %s (leaving pending, no delivery): %v", id, err)
			return deliverError
		}
	}
	var hydration companyHydration
	if len(r.Hydration) > 0 {
		_ = json.Unmarshal(r.Hydration, &hydration)
	}
	threadRootTS := receiptRootTS(r, msg)

	// Deliver each still-pending recorded target (frozen route). Results are
	// collected in memory and applied in a single finalize commit. removeKeys
	// carries the stale keys of any targets re-resolved to a new session so the
	// finalize commit drops them from the target map.
	now := g.now().UTC()
	results := make(map[string]TargetDelivery, len(r.Targets))
	removeKeys := make(map[string]bool)
	for key, td := range r.Targets {
		if td.Status != companyTargetPending || td.Session == "" {
			continue
		}
		if td.Attempts >= companyMaxDeliveryAttempts {
			// Bounded-attempts cap: a target that has failed this many times
			// is a terminal, visible failure rather than an immortal redrive.
			td.Status = companyTargetFailed
			td.Detail = fmt.Sprintf("%s after %d attempts: %s", companyReasonAttemptsExhausted, td.Attempts, td.Detail)
			td.UpdatedAt = now
			results[key] = td
			g.deliveryFailures.Add(1)
			log.Printf("company: delivery exhausted receipt=%s session=%s attempts=%d", id, td.Session, td.Attempts)
			continue
		}
		// Advisory session-existence guard (Phase 4, gated; applies to room and
		// DM deliveries equally): a 404/409 leaves the target pending (do NOT
		// post) with the guard detail, consuming one attempt so the cap still
		// bounds the loop; the sweep re-checks. A guard error or flag-off never
		// blocks.
		if blocked, detail := g.sessionGuardBlock(td.City, td.Session); blocked {
			td.Attempts++
			td.UpdatedAt = now
			td.Status = companyTargetPending
			td.Detail = detail
			if detail == companyDetailSessionMissing {
				// Self-heal a session-not-found hold: re-resolve a stale binding or
				// materialize the cold session. Never fails the target — the notice
				// stays suppressed while the attempts budget remains.
				heal := g.healSessionMissing(r, room, key, td)
				recordHeal(results, removeKeys, key, heal)
				log.Printf("company: session guard held+heal receipt=%s session=%s attempts=%d detail=%s", id, heal.td.Session, heal.td.Attempts, heal.td.Detail)
				continue
			}
			results[key] = td
			log.Printf("company: session guard held receipt=%s session=%s attempts=%d detail=%s", id, td.Session, td.Attempts, detail)
			continue
		}
		// The current-turn pointer is written atomically BEFORE the gc POST on
		// every wake, so the Python verbs have deterministic context the
		// instant the turn can act. A pointer-write failure is retryable: skip
		// the POST and leave the target pending for the sweep.
		ptr := companyPointerFromTarget(r, room, td, threadRootTS, now)
		if perr := writeCurrentTurnPointer(g.turnsDir, ptr); perr != nil {
			td.Attempts++
			td.UpdatedAt = now
			td.Status = companyTargetPending
			td.Detail = "current-turn pointer write: " + perr.Error()
			results[key] = td
			g.deliveryFailures.Add(1)
			log.Printf("company: pointer write receipt=%s session=%s: %v", id, td.Session, perr)
			continue
		}
		body := renderCompanyReminder(room, companyReminderAuthorClass(td.Kind), td.Kind, msg.Text, origin.TS, threadRootTS, hydration, r.Synthesis)
		disp, detail := g.postCompanyBody(td, body)
		td.Attempts++
		td.UpdatedAt = now
		switch disp {
		case postDelivered:
			td.Status = companyTargetDelivered
			td.Detail = ""
			results[key] = td
		case postRetryable:
			// Timeout / connection error / 5xx / 408 / 429: leave pending for
			// the sweep to retry with the same key.
			td.Status = companyTargetPending
			td.Detail = detail
			g.deliveryFailures.Add(1)
			log.Printf("company: delivery pending receipt=%s session=%s attempts=%d: %s", id, td.Session, td.Attempts, detail)
			results[key] = td
		case postSessionMissing:
			// 404: the bound session vanished between the guard check and the POST
			// (or the guard is off). Keep it pending and self-heal — re-resolve a
			// stale binding or materialize the cold session — never a failure.
			td.Status = companyTargetPending
			td.Detail = companyDetailSessionMissing
			heal := g.healSessionMissing(r, room, key, td)
			recordHeal(results, removeKeys, key, heal)
			log.Printf("company: delivery session-missing+heal receipt=%s session=%s attempts=%d detail=%s", id, heal.td.Session, heal.td.Attempts, heal.td.Detail)
		default:
			// Definitive 4xx (not 404/408/429): gc rejected the submission on its
			// merits — mark the target failed rather than retry forever.
			td.Status = companyTargetFailed
			td.Detail = detail
			g.deliveryFailures.Add(1)
			log.Printf("company: delivery failed receipt=%s session=%s attempts=%d: %s", id, td.Session, td.Attempts, detail)
			results[key] = td
		}
	}

	if err := g.commitReceipt(r, func(cur *IngressReceipt) {
		if cur.Targets == nil {
			cur.Targets = make(map[string]TargetDelivery, len(results))
		}
		for k := range removeKeys {
			delete(cur.Targets, k)
		}
		for k, v := range results {
			cur.Targets[k] = v
		}
		status, reason := computeReceiptStatus(cur.Targets)
		cur.Status = status
		cur.Reason = reason
	}); err != nil {
		log.Printf("company: finalize %s: %v", id, err)
		return deliverError
	}
	if isTerminalStatus(r.Status) {
		// delivered / failed / no_delivery: run the terminal visible-ack hook
		// (👀→✅ / 👀→⚠️+threaded reply / removes-only), then advance the chain.
		g.applyTerminalAck(r)
		return deliverTerminal
	}
	// Still routing (a pending/retryable target remains): abort the chain
	// remainder — the next sweep pass rebuilds and resumes.
	return deliverPending
}

// parkAdvanceOutcome classifies a freezeWakes-park exit: a correlation park
// that terminalized under the S7 budget (correlation_recovery_exhausted) is a
// committed terminal state; any other park is pre-claim. Both advance the chain,
// but the distinction keeps the outcome honest for the sequencer and tests.
func (g *companyGateway) parkAdvanceOutcome(r *IngressReceipt) deliverOutcome {
	if isTerminalStatus(r.Status) {
		return deliverTerminal
	}
	return deliverParkedPreclaim
}

// frozenWake is a routing decision resolved to (agent, kind, delegation_key),
// ready to freeze into a TargetDelivery. Human legs (ambient/targeted) and the
// uncorrelated peer_input leg carry an empty DelegationKey; the correlated
// peer_delegation / peer_result legs carry the delegation-record filename.
type frozenWake struct {
	Agent         CompanyAgent
	Kind          string
	DelegationKey string
	// Snapshot is the frozen synthesis snapshot for the peer_result leg
	// (non-nil iff Kind is peer_result); deliverReceipt marshals it into the
	// receipt's Synthesis field in the routing commit that freezes targets.
	Snapshot *companySynthesisSnapshot
}

// freezeWakes converts a RouteDecision into frozen wakes. Human decisions map
// one-to-one. A company-bot decision runs the peer correlation layer, which
// refines each wake to peer_delegation, peer_result (claiming the record on a
// metadata-gated result), or keyless peer_input, and can request a fail-closed
// park (returned as a non-empty reason).
//
// The five-condition trust checklist is applied as an assertion, not a filter:
// ComputeWakeSet's company-bot leg already enforces conditions 1-4 and
// member/eligibility, and the binding condition is handled downstream by
// ensureTargets (an unbound receiver becomes a recorded failed target, never a
// silent drop — mirroring the human legs). A checklist failure here would mean
// the router and the checklist disagree, so it is logged as a defense signal.
func (g *companyGateway) freezeWakes(dir *CompanyDirectory, room *CompanyRoom, msg CompanyMessage, decision RouteDecision, authorAgent *CompanyAgent) (frozen []frozenWake, parkReason string) {
	if decision.Author != AuthorCompanyBot || authorAgent == nil {
		for _, wt := range decision.Wakes {
			frozen = append(frozen, frozenWake{Agent: wt.Agent, Kind: wt.Kind})
		}
		return frozen, ""
	}
	bindings := g.bindStore.Snapshot()
	mentionSet := make(map[string]bool)
	for _, id := range ExtractMentionIDs(msg.Blocks, msg.Text) {
		mentionSet[id] = true
	}
	for _, wt := range decision.Wakes {
		agent := wt.Agent
		if reason := evaluatePeerTrust(dir, bindings, room, authorAgent, &agent, mentionSet); reason != peerTrustOK && reason != peerTrustReceiverUnbound {
			// Only the unbound case is a legitimate downstream failure; any
			// other disagreement is a routing invariant violation worth a log.
			log.Printf("company: peer trust checklist flagged author=%s receiver=%s: %s", authorAgent.Name, wt.Agent.Name, reason)
		}
	}
	peerWakes, park, err := g.peerEnv().resolvePeerWakes(msg, authorAgent, decision.Wakes)
	if err != nil {
		// S7: a correlation-layer I/O or scan error (ReadDir, lock open/flock,
		// intent scan) must NOT consume the attempt budget. Park under the
		// distinct, non-counting correlation_error reason (plain every-sweep
		// cadence) rather than the budget-consuming correlation_pending, so a
		// transient degraded-infra window can never terminalize a trusted,
		// claimable result. This mirrors the S1 relock-exhaustion path
		// (resolveResultWake), which already returns peerParkCorrelationError.
		log.Printf("company: peer correlation error: %v", err)
		return nil, peerParkCorrelationError
	}
	if park != "" {
		return nil, park
	}
	for _, pw := range peerWakes {
		frozen = append(frozen, frozenWake{Agent: pw.Agent, Kind: pw.Kind, DelegationKey: pw.DelegationKey, Snapshot: pw.Snapshot})
	}
	return frozen, ""
}

// ensureTargets adds a TargetDelivery for every frozen wake not already
// recorded, preserving the state of any target from a prior attempt (a
// delivered target stays delivered). Bound and unbound targets live under
// disjoint key namespaces (companyBoundTargetKeyPrefix /
// companyUnboundTargetKeyPrefix) so no session name — however pathological —
// can collide with a failed-unbound record. The idempotency key still derives
// from the raw session.
func (g *companyGateway) ensureTargets(r *IngressReceipt, room *CompanyRoom, wakes []frozenWake, bindings *CompanyBindings, now time.Time) {
	if r.Targets == nil {
		r.Targets = make(map[string]TargetDelivery, len(wakes))
	}
	for _, wt := range wakes {
		var session, targetCity string
		binding, bound := bindings.BindingFor(room.Name, wt.Agent.Name)
		if bound {
			session, targetCity = binding.Session, binding.City
		}
		if !bound {
			key := companyUnboundTargetKeyPrefix + wt.Agent.Name
			if _, exists := r.Targets[key]; exists {
				continue
			}
			r.Targets[key] = TargetDelivery{
				Kind:          wt.Kind,
				Status:        companyTargetFailed,
				Detail:        fmt.Sprintf("no company binding for (room=%s, agent=%s)", room.Name, wt.Agent.Name),
				UpdatedAt:     now,
				Agent:         wt.Agent.Name,
				DelegationKey: wt.DelegationKey,
			}
			continue
		}
		key := companyBoundTargetKeyPrefix + session
		if _, exists := r.Targets[key]; exists {
			continue // preserve prior attempt state
		}
		r.Targets[key] = TargetDelivery{
			Session:        session,
			City:           targetCity,
			Kind:           wt.Kind,
			Status:         companyTargetPending,
			IdempotencyKey: companyIdempotencyKey(r.ID, session),
			UpdatedAt:      now,
			Agent:          wt.Agent.Name,
			DelegationKey:  wt.DelegationKey,
		}
	}
}

// isBotAuthored reports whether a message is bot-authored (bot_message
// subtype or a non-empty bot id).
func isBotAuthored(msg CompanyMessage) bool {
	return msg.Subtype == "bot_message" || msg.BotID != ""
}

// hasPendingBoundTarget reports whether any recorded target is a pending bound
// session (i.e. there is someone to hydrate + deliver to).
func hasPendingBoundTarget(r *IngressReceipt) bool {
	for _, td := range r.Targets {
		if td.Status == companyTargetPending && td.Session != "" {
			return true
		}
	}
	return false
}

// companyReminderAuthorClass derives the reminder's author label from the
// frozen wake kind, so redrives never depend on re-resolving the author.
func companyReminderAuthorClass(kind string) string {
	if isPeerKind(kind) {
		return "company_bot"
	}
	return "human"
}

// companyIdempotencyKey builds the per-target key carried as the
// Idempotency-Key header on the session submission.
func companyIdempotencyKey(receiptID, session string) string {
	return "ingress:" + receiptID + ":target:" + session
}

// computeReceiptStatus derives the receipt-level status from its targets:
// any pending target keeps the receipt non-terminal (routing); otherwise
// any failed target makes it failed; all delivered makes it delivered.
func computeReceiptStatus(targets map[string]TargetDelivery) (status, reason string) {
	anyPending, failed := false, 0
	for _, t := range targets {
		switch t.Status {
		case companyTargetDelivered:
		case companyTargetFailed:
			failed++
		default:
			anyPending = true
		}
	}
	if anyPending {
		return ingressStatusRouting, ""
	}
	if failed > 0 {
		return ingressStatusFailed, fmt.Sprintf("%d target(s) failed delivery", failed)
	}
	return ingressStatusDelivered, ""
}

// postDisposition classifies the outcome of a session-message POST so the
// delivery loop can route a session-not-found 404 into the self-heal pipeline
// instead of terminalizing it as a definitive failure.
type postDisposition int

const (
	postDelivered      postDisposition = iota // gc acknowledged 2xx
	postRetryable                             // timeout / connection error / 5xx / 408 / 429 — retry same key
	postSessionMissing                        // 404 — the bound session does not exist (self-heal path)
	postDefinitive                            // any other definitive 4xx / unrecoverable construction error
)

// postCompanyBody POSTs an already-rendered reminder body to the bound
// session's gc messages endpoint with the target's Idempotency-Key. It
// returns (disposition, detail):
//   - postDelivered only on gc's acknowledged 2xx.
//   - postRetryable for outcomes whose success is unknown or transient —
//     timeout, connection error, 5xx, 408, 429 — which stay pending for the
//     sweep to retry with the same key.
//   - postSessionMissing for a 404: the bound session does not exist. The
//     caller keeps the target pending and runs the self-heal pipeline
//     (re-resolution / materialization) rather than failing it.
//   - postDefinitive for any other definitive rejection: another 4xx, or an
//     unrecoverable request-construction error. The caller marks the target
//     failed rather than retrying forever.
func (g *companyGateway) postCompanyBody(td TargetDelivery, body string) (postDisposition, string) {
	payload, err := json.Marshal(gcSessionMessageRequest{Message: body})
	if err != nil {
		// Deterministic construction failure: retrying cannot help.
		return postDefinitive, "marshal session-message body: " + err.Error()
	}
	// City-qualified bindings deliver to sessions in other gc cities; each
	// city runs its own supervisor, so the target city selects both the URL
	// path segment and the API base (SLACK_COMPANY_CITY_APIS). An empty
	// City means the adapter's own city and base. A city with no configured
	// base is a definitive configuration failure — retrying cannot help
	// until the operator fixes the map and redrives.
	targetCity, apiBase := td.City, g.cfg.gcAPIBase
	if targetCity == "" {
		targetCity = g.cfg.cityName
	} else if targetCity != g.cfg.cityName {
		mapped, ok := g.cfg.companyCityAPIs[targetCity]
		if !ok {
			return postDefinitive, fmt.Sprintf("no SLACK_COMPANY_CITY_APIS entry for city %q", targetCity)
		}
		apiBase = mapped
	}
	target := fmt.Sprintf("%s/v0/city/%s/session/%s/messages",
		apiBase, url.PathEscape(targetCity), url.PathEscape(td.Session))
	req, err := http.NewRequest(http.MethodPost, target, bytes.NewReader(payload))
	if err != nil {
		return postDefinitive, "build request: " + err.Error()
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GC-Request", companyDeliverRequestTag)
	req.Header.Set("Idempotency-Key", td.IdempotencyKey)

	resp, err := g.deliverClient.Do(req)
	if err != nil {
		// Timeout / connection error: outcome unknown, retry.
		return postRetryable, "POST: " + err.Error()
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return postDelivered, ""
	}
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, companyMaxErrorBodyBytesRead))
	detail := fmt.Sprintf("%s: %s", resp.Status, strings.TrimSpace(string(respBody)))
	switch {
	case resp.StatusCode == http.StatusNotFound:
		// 404: the bound session does not exist. Recoverable — the caller keeps
		// the target pending and runs the self-heal pipeline (re-resolve the
		// stale binding, or materialize the cold session) rather than failing it.
		return postSessionMissing, detail
	case resp.StatusCode == http.StatusRequestTimeout || resp.StatusCode == http.StatusTooManyRequests:
		// 408 / 429: transient, retry.
		return postRetryable, detail
	case resp.StatusCode >= 400 && resp.StatusCode < 500:
		// Definitive 4xx: rejected on its merits, do not retry.
		return postDefinitive, detail
	default:
		// 5xx and anything else non-2xx: retry.
		return postRetryable, detail
	}
}

// companySessionCreateRequest is the POST /v0/city/{city}/sessions body used to
// materialize a cold named/pool session. name is the template/pool name; kind is
// always "agent" (the only kind the adapter materializes).
type companySessionCreateRequest struct {
	Name string `json:"name"`
	Kind string `json:"kind"`
}

// companySessionRecord is the subset of the supervisor's GET session record the
// self-heal path reads: the template a non-template-named instance was minted
// from, used to derive the materialization name when the bound session itself
// does not look like a template/pool name.
type companySessionRecord struct {
	Template string `json:"template,omitempty"`
}

// looksLikeTemplateName reports whether a session name looks like a
// template/pool name that gc can materialize directly: a config-form dotted
// name (e.g. "teams.pm") or its dunder runtime alias (e.g. "teams__pm"). An
// adhoc instance id (no dot, no dunder) is not materializable by name and must
// fall back to the supervisor record's template field.
func looksLikeTemplateName(session string) bool {
	return strings.Contains(session, ".") || strings.Contains(session, "__")
}

// currentBindingSession resolves the CURRENT bound session (and its city
// qualifier) for a target's (agent, room-or-dm) from the loaded registries. A
// dm-family receipt resolves through dm_bindings (keyed by agent); a room
// receipt through the room bindings (keyed by room+agent). bound=false means the
// binding row is gone — the caller must NOT re-resolve or materialize (unbound
// semantics stay intact). This mirrors applyRedrive's resolver so the retry path
// and the operator redrive re-resolve identically.
func (g *companyGateway) currentBindingSession(r *IngressReceipt, room *CompanyRoom, agent string) (session, city string, bound bool) {
	if agent == "" {
		return "", "", false
	}
	if isDMFamilyKind(r.Kind) {
		if bd, ok := g.dmBindStore.Snapshot().BindingFor(agent); ok {
			return bd.Session, bd.City, true
		}
		return "", "", false
	}
	if room != nil {
		if bd, ok := g.bindStore.Snapshot().BindingFor(room.Name, agent); ok {
			return bd.Session, bd.City, true
		}
	}
	return "", "", false
}

// sessionMissingHealResult carries the outcome of one self-heal pass. td is the
// (possibly re-resolved) target to record; when rekeyed is set the target moved
// to a new bound-key namespace (newKey) and the stale oldKey must be dropped from
// the receipt's target map in the finalize commit.
type sessionMissingHealResult struct {
	td      TargetDelivery
	newKey  string
	oldKey  string
	rekeyed bool
}

// healSessionMissing runs the self-heal pipeline for a BOUND target whose current
// delivery attempt hit a session-not-found condition — the advisory guard's 404
// hold or a delivery POST 404. It never fails the target: the caller keeps it
// pending so no user-visible failure notice fires while the attempts budget
// remains (P0 §failure-notice suppression).
//
//   - Auto-re-resolution (P0 item 1): if the current binding for this target's
//     (agent, room/dm) names a DIFFERENT session than the frozen one, the target
//     adopts the current binding (session, city, freshly-derived idempotency key)
//     and is re-keyed. The next sweep probes the live session. Attempts are NOT
//     reset — the budget still bounds a binding that keeps churning.
//   - Auto-materialization (P0 item 2): if the binding still names this exact
//     (cold) session, fire at most ONE materialize POST per (city, session) per
//     sweep interval.
//   - Binding row gone: neither re-resolve nor materialize (unbound semantics).
//
// The caller has already set td.Status=pending, bumped Attempts, and stamped
// UpdatedAt; healSessionMissing owns only the Session/City/IdempotencyKey rewrite
// and the recoverable Detail.
func (g *companyGateway) healSessionMissing(r *IngressReceipt, room *CompanyRoom, key string, td TargetDelivery) sessionMissingHealResult {
	session, city, bound := g.currentBindingSession(r, room, td.Agent)
	switch {
	case bound && session != "" && session != td.Session:
		old := td.Session
		td.Session = session
		td.City = city
		td.IdempotencyKey = companyIdempotencyKey(r.ID, session)
		td.Detail = companyDetailSessionMissing // stays guard-held → swept on the 60s cadence
		g.targetReresolved.Add(1)
		log.Printf("company: target re-resolved receipt=%s agent=%s session %s->%s (stale frozen binding)",
			r.ID, td.Agent, old, session)
		return sessionMissingHealResult{td: td, newKey: companyBoundTargetKeyPrefix + session, oldKey: key, rekeyed: true}
	case bound && session == td.Session:
		if g.tryMaterialize(r, td) {
			td.Detail = companyDetailMaterializing
		} else {
			td.Detail = companyDetailSessionMissing
		}
		return sessionMissingHealResult{td: td}
	default:
		// Binding row gone: leave the target pending under session_missing. It is
		// no longer re-resolvable and ages out under the attempts cap; we never
		// fabricate a session for a vanished binding (unbound semantics).
		td.Detail = companyDetailSessionMissing
		return sessionMissingHealResult{td: td}
	}
}

// recordHeal merges one self-heal pass into the delivery loop's in-memory
// results, handling re-keying: a re-resolved target lands under its new bound
// key and the stale key is queued for deletion in the finalize commit.
func recordHeal(results map[string]TargetDelivery, removeKeys map[string]bool, key string, heal sessionMissingHealResult) {
	if heal.rekeyed {
		removeKeys[heal.oldKey] = true
		results[heal.newKey] = heal.td
		return
	}
	results[key] = heal.td
}

// tryMaterialize fires at most ONE session-materialization POST per (city,
// session) per sweep interval for a cold bound target stuck in session_missing.
// It POSTs /v0/city/{city}/sessions {"name": <n>, "kind": "agent"} with an
// Idempotency-Key of materialize:<receipt>:<session>, where <n> is the binding
// session when it looks like a template/pool name, else the supervisor record's
// template. It reports whether a POST was fired this pass (false = throttled or
// no derivable name). Best-effort: any transport/status outcome leaves the target
// pending for the next sweep to re-probe — the attempts budget bounds everything.
func (g *companyGateway) tryMaterialize(r *IngressReceipt, td TargetDelivery) bool {
	targetCity := td.City
	if targetCity == "" {
		targetCity = g.cfg.cityName
	}
	// Throttle gate: record the attempt under the lock BEFORE any work so two
	// receipts racing the same cold session can never double-fire within a sweep
	// interval (the strongest at-most-once guarantee).
	throttleKey := targetCity + "\x00" + td.Session
	g.materializeMu.Lock()
	if last, seen := g.materializeAttempts[throttleKey]; seen && g.now().Sub(last) < g.sweepInterval {
		g.materializeMu.Unlock()
		return false
	}
	g.materializeAttempts[throttleKey] = g.now()
	g.materializeMu.Unlock()

	apiBase := g.cfg.gcAPIBase
	if td.City != "" && td.City != g.cfg.cityName {
		mapped, ok := g.cfg.companyCityAPIs[td.City]
		if !ok {
			// No configured base for this city — the delivery POST already surfaces
			// the misconfiguration definitively; nothing to materialize against.
			return false
		}
		apiBase = mapped
	}

	name := g.materializeName(apiBase, targetCity, td.Session)
	if name == "" {
		log.Printf("company: materialize skip receipt=%s session=%s: no derivable template/pool name",
			r.ID, td.Session)
		return false
	}

	payload, err := json.Marshal(companySessionCreateRequest{Name: name, Kind: "agent"})
	if err != nil {
		return false
	}
	target := fmt.Sprintf("%s/v0/city/%s/sessions", apiBase, url.PathEscape(targetCity))
	req, err := http.NewRequest(http.MethodPost, target, bytes.NewReader(payload))
	if err != nil {
		return false
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GC-Request", companyDeliverRequestTag)
	req.Header.Set("Idempotency-Key", "materialize:"+r.ID+":"+td.Session)

	g.materializeRequests.Add(1)
	resp, err := g.deliverClient.Do(req)
	if err != nil {
		log.Printf("company: materialize POST receipt=%s city=%s session=%s name=%s: %v",
			r.ID, targetCity, td.Session, name, err)
		return true
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, companyMaxErrorBodyBytesRead))
	log.Printf("company: materialize requested receipt=%s city=%s session=%s name=%s status=%d",
		r.ID, targetCity, td.Session, name, resp.StatusCode)
	return true
}

// materializeName derives the session-create "name" for a materialization POST:
// the bound session itself when it looks like a template/pool name (config-form
// dotted name or dunder runtime alias), else the "template" field of the
// supervisor's session record when one exists. Empty when neither yields a name.
func (g *companyGateway) materializeName(apiBase, city, session string) string {
	if looksLikeTemplateName(session) {
		return session
	}
	return g.fetchSessionTemplate(apiBase, city, session)
}

// fetchSessionTemplate best-effort GETs the supervisor session record and returns
// its template field, or "" on any error / absent record / empty template. Used
// only when the bound session name is not itself a materializable template/pool
// name.
func (g *companyGateway) fetchSessionTemplate(apiBase, city, session string) string {
	target := fmt.Sprintf("%s/v0/city/%s/session/%s", apiBase, url.PathEscape(city), url.PathEscape(session))
	req, err := http.NewRequest(http.MethodGet, target, nil)
	if err != nil {
		return ""
	}
	req.Header.Set("X-GC-Request", companyDeliverRequestTag)
	resp, err := g.deliverClient.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, companyMaxErrorBodyBytesRead))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return ""
	}
	var rec companySessionRecord
	if err := json.Unmarshal(body, &rec); err != nil {
		return ""
	}
	return rec.Template
}

// decodeCompanyMessage reconstructs the router's CompanyMessage view from
// the stored inner event. The origin supplies the canonical keys; the
// decoded event supplies the routing fields (subtype, author, text,
// blocks, thread, app/metadata for the bot legs).
func decodeCompanyMessage(origin ReceiptOrigin, event json.RawMessage) CompanyMessage {
	var ev slackMessageEvent
	_ = json.Unmarshal(event, &ev)
	return CompanyMessage{
		TeamID:          origin.TeamID,
		ChannelID:       origin.ChannelID,
		TS:              origin.TS,
		ThreadTS:        ev.ThreadTS,
		AppID:           ev.AppID,
		BotProfileAppID: parseBotProfileAppID(ev.BotProfile),
		Metadata:        ev.Metadata,
		UserID:          ev.User,
		BotID:           ev.BotID,
		Subtype:         ev.Subtype,
		Text:            ev.Text,
		Blocks:          ev.Blocks,
	}
}

// parkReceipt records the directory-park state: non-terminal, Status
// "received", Reason "parked_no_directory_room".
func (g *companyGateway) parkReceipt(r *IngressReceipt) {
	g.parkWithReason(r, companyReasonParked)
}

// parkWithReason records a non-terminal parked state carrying a
// machine-readable reason (directory park, author-resolution-pending, or a
// peer correlation park). For the two backoff reasons (correlation_pending and
// ambiguous_pending_delegations) it drives the S7 attempt schedule via
// parkWithRecovery; for every other reason it stays idempotent — an already-
// parked receipt with the same reason is left untouched to avoid generation
// churn on every sweep (correlation_error, author_resolution_pending, and the
// directory park all keep their plain every-sweep cadence).
func (g *companyGateway) parkWithReason(r *IngressReceipt, reason string) {
	if isRecoveryReason(reason) {
		g.parkWithRecovery(r, reason)
		return
	}
	if r.Status == ingressStatusReceived && r.Reason == reason {
		return
	}
	log.Printf("company: parking receipt %s reason=%s", r.ID, reason)
	if err := g.commitReceipt(r, func(cur *IngressReceipt) {
		cur.Status = ingressStatusReceived
		cur.Reason = reason
	}); err != nil {
		log.Printf("company: park receipt %s: %v", r.ID, err)
	}
}

// isRecoveryReason reports whether a park reason is subject to the S7 bounded
// backoff. correlation_error and the Phase 1/2 parks explicitly are not: they
// keep the plain every-sweep cadence and never consume the attempt budget.
func isRecoveryReason(reason string) bool {
	return reason == peerParkCorrelationPending || reason == peerParkAmbiguousPending
}

// isCorrelationParked reports whether a receipt is currently parked under a
// correlation reason counted on /healthz as company_correlation_parked
// (correlation_pending or ambiguous_pending_delegations).
func isCorrelationParked(r *IngressReceipt) bool {
	return r.Status == ingressStatusReceived &&
		(r.Reason == peerParkCorrelationPending || r.Reason == peerParkAmbiguousPending)
}

// parkWithRecovery drives the S7 correlation-park backoff. The first park under
// a recovery reason is immediately eligible on the next pass (no initial delay,
// attempts unchanged). A re-park under the SAME reason is exactly the case
// "a redrive re-ran resolveResultWake and found the posting intent still in
// flight" (the idempotent-early-return case): it counts one attempt and either
// schedules the next backed-off pass or terminalizes. correlation_pending goes
// terminal (failed / correlation_recovery_exhausted, counted in
// deliveryFailures) at attempt 6; the Slack-born ambiguous park (D5) uses the
// same schedule but NEVER terminalizes — it keeps retrying at the 15-minute cap.
func (g *companyGateway) parkWithRecovery(r *IngressReceipt, reason string) {
	alreadyParked := r.Status == ingressStatusReceived && r.Reason == reason
	if !alreadyParked {
		// First park under this recovery reason: immediately eligible next pass.
		// The attempt budget is scoped PER reason — a transition to a different
		// recovery reason (or from a non-recovery park, e.g. a correlation_error
		// interlude) starts a fresh streak, so an unrelated prior streak (a
		// long-lived ambiguous park, or a burst of correlation_error re-parks)
		// can never near-instantly terminalize this genuine correlation_pending.
		log.Printf("company: parking receipt %s reason=%s (recovery)", r.ID, reason)
		if err := g.commitReceipt(r, func(cur *IngressReceipt) {
			cur.Status = ingressStatusReceived
			cur.Reason = reason
			cur.RecoveryReason = reason
			cur.RecoveryAttempts = 0
			cur.RecoveryNextAt = time.Time{}
		}); err != nil {
			log.Printf("company: park receipt %s: %v", r.ID, err)
		}
		return
	}
	attempts := r.RecoveryAttempts + 1
	if reason == peerParkCorrelationPending && attempts >= companyPeerRecoveryMaxAttempts {
		log.Printf("company: correlation park exhausted receipt=%s attempts=%d -> terminal", r.ID, attempts)
		if err := g.commitReceipt(r, func(cur *IngressReceipt) {
			cur.Status = ingressStatusFailed
			cur.Reason = companyReasonRecoveryExhausted
			cur.RecoveryReason = reason
			cur.RecoveryAttempts = attempts
			cur.RecoveryNextAt = time.Time{}
		}); err != nil {
			log.Printf("company: terminalize correlation park %s: %v", r.ID, err)
			return
		}
		g.deliveryFailures.Add(1)
		g.applyTerminalAck(r)
		return
	}
	next := g.now().UTC().Add(nextRecoveryDelay(attempts))
	log.Printf("company: correlation park backoff receipt=%s reason=%s attempts=%d next=%s", r.ID, reason, attempts, next.Format(time.RFC3339))
	if err := g.commitReceipt(r, func(cur *IngressReceipt) {
		cur.Status = ingressStatusReceived
		cur.Reason = reason
		cur.RecoveryReason = reason
		cur.RecoveryAttempts = attempts
		cur.RecoveryNextAt = next
	}); err != nil {
		log.Printf("company: schedule correlation park %s: %v", r.ID, err)
	}
}

// commitReceipt applies apply(r) and persists via a generation-checked
// Update. On ErrStale it re-reads the on-disk receipt, re-applies the
// intent onto that fresh base (a merge, not a blind overwrite), and
// retries exactly once.
func (g *companyGateway) commitReceipt(r *IngressReceipt, apply func(cur *IngressReceipt)) error {
	store := g.store()
	if store == nil {
		return errors.New("company: receipt store unavailable")
	}
	apply(r)
	err := store.Update(r)
	if err == nil {
		return nil
	}
	if !errors.Is(err, ErrStale) {
		return err
	}
	fresh, gerr := store.Get(r.Origin)
	if gerr != nil {
		return gerr
	}
	if fresh == nil {
		return fmt.Errorf("company: receipt %s vanished during update", r.ID)
	}
	*r = *fresh
	apply(r)
	return store.Update(r)
}

func (g *companyGateway) acquireSingleFlight(id string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.inflight[id] {
		return false
	}
	g.inflight[id] = true
	return true
}

func (g *companyGateway) releaseSingleFlight(id string) {
	g.mu.Lock()
	delete(g.inflight, id)
	g.mu.Unlock()
}

// startRecovery runs the company-scoped startup barrier: it (re)constructs
// the receipt store if the gateway started degraded, then retries the
// synchronous Pending() scan until it succeeds, opens the admission
// barrier, and starts the periodic sweep (which waits one interval before
// its first pass). While the store cannot be constructed the barrier stays
// closed, so company-admissible events get 503 (retryable) — never legacy
// fallthrough — and /healthz reports the store error. A nil gateway is a
// no-op.
func (g *companyGateway) startRecovery(ctx context.Context) {
	if g == nil {
		return
	}
	go func() {
		for {
			if _, err := g.ensureStore(); err != nil {
				log.Printf("company: receipt store unavailable (degraded): %v; retrying in %s", err, companyRecoveryRetryInterval)
				select {
				case <-ctx.Done():
					return
				case <-time.After(companyRecoveryRetryInterval):
					continue
				}
			}
			if err := g.recoverPending(); err != nil {
				log.Printf("company: startup recovery scan failed: %v; retrying in %s", err, companyRecoveryRetryInterval)
				select {
				case <-ctx.Done():
					return
				case <-time.After(companyRecoveryRetryInterval):
					continue
				}
			}
			g.barrier.Store(true)
			log.Printf("company: startup recovery complete; admission barrier open")
			go g.runSweep(ctx)
			return
		}
	}()
}

// recoverPending runs one synchronous Pending() scan and enqueues every
// eligible non-terminal receipt for delivery. Per-root replay chains
// (result-bearing receipts and correlation parks) are ordered per S5 and driven
// sequentially by one worker chain; every other receipt keeps the store's
// (ReceivedAt, Origin.TS) order and is triggered concurrently as before. It
// applies the same recoveryDue eligibility as the sweep so a restart neither
// bypasses the S7 backoff nor bumps attempts. It does NOT open the barrier — the
// caller does that on success — so tests can drive it directly.
func (g *companyGateway) recoverPending() error {
	store := g.store()
	if store == nil {
		return errors.New("company: receipt store unavailable")
	}
	pending, err := store.Pending()
	if err != nil {
		return err
	}
	now := g.now()
	var eligible []*IngressReceipt
	for _, rec := range pending {
		if !recoveryDue(rec, now) {
			continue
		}
		eligible = append(eligible, rec)
	}
	chains, rest := g.orderPendingForReplay(eligible)
	for _, c := range chains {
		g.triggerChain(c)
	}
	for _, rec := range rest {
		g.triggerDelivery(rec.Origin)
	}
	return nil
}

// triggerChain acquires one dispatch slot for a whole replay chain and drives
// it sequentially and synchronously in a goroutine (deliverChain). The single
// slot bounds a chain to the same process-wide backpressure as an individual
// delivery; on saturation the chain is left durably pending for the next sweep.
func (g *companyGateway) triggerChain(c replayChain) {
	if g == nil || len(c.Origins) == 0 {
		return
	}
	release, _, ok := g.cfg.tryAcquireDispatchSlot()
	if !ok {
		log.Printf("company: dispatch slot unavailable; replay chain of %d left pending for sweep", len(c.Origins))
		return
	}
	g.deliverWG.Add(1)
	go func() {
		defer g.deliverWG.Done()
		defer release()
		g.deliverChain(c)
	}()
}

// runSweep is the periodic redrive loop. It waits one interval before its
// first pass (NewTicker semantics) and exits on ctx cancel.
func (g *companyGateway) runSweep(ctx context.Context) {
	ticker := time.NewTicker(g.sweepInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			g.sweepOnce()
		}
	}
}

// sweepOnce GC's terminal receipts past retention and redrives eligible
// non-terminal receipts (parked, pending, or a stale routing claim) in a
// single directory scan (SweepAndPending).
func (g *companyGateway) sweepOnce() {
	store := g.store()
	if store == nil {
		return
	}
	pending, healNeeded, _, dmCounts, bodyCounts, err := store.SweepAndPending(g.retention)
	if err != nil {
		log.Printf("company: sweep: %v", err)
		return
	}
	now := g.now()
	parked := 0
	var eligible []*IngressReceipt
	for _, rec := range pending {
		if isCorrelationParked(rec) {
			// Count every correlation/ambiguity park (eligible or backed-off) for
			// the /healthz operator signal — computed in this existing scan (S7).
			parked++
		}
		if !sweepEligible(rec, now, g.staleWindow) {
			continue
		}
		eligible = append(eligible, rec)
	}
	g.companyCorrelationParked.Store(int64(parked))
	// Per-root replay chains (S5) are driven sequentially; every other eligible
	// receipt keeps store order and is triggered concurrently as before.
	chains, rest := g.orderPendingForReplay(eligible)
	for _, c := range chains {
		g.triggerChain(c)
	}
	for _, rec := range rest {
		g.triggerDelivery(rec.Origin)
	}
	// Terminal-ack sweep-healing (Phase 3b): a terminal receipt stranded on
	// AckState=="eyes"/"warned" (its terminal ack was rate-limited or the process
	// crashed between finalize and the ack commit) is healed here, within
	// retention. The heal set rides the same SweepAndPending scan (no second
	// full-store pass); it is applied only when acks are enabled — the default
	// (off) pays nothing.
	if g.visibleAcks {
		for _, r := range healNeeded {
			g.applyTerminalAck(r)
		}
	}
	// Read-only scan of the intents dir for the /healthz operator signal:
	// intents stuck in "posting" past their retry_deadline (a wedged Python
	// outbound flow). The Go side never reposts — this is a count only.
	g.stalePostingIntents.Store(int64(g.peerEnv().countStalePostingIntents()))
	// Phase 4 DM receipt gauge (by status) for /healthz, computed inside the
	// single SweepAndPending scan above (no second full-store pass under the
	// store mutex — m8).
	g.dmStatusCounts.Store(&dmCounts)
	// Phase 5 body-integrity gauge (missing / redacted) for /healthz, computed
	// inside the same SweepAndPending scan — no second directory pass (m8).
	g.bodyIntegrity.Store(&bodyCounts)
}

// sweepEligible reports whether the sweep should redrive a non-terminal
// receipt. Unclaimed/pending receipts (Status != routing: received or
// parked) are always eligible; a claimed (routing) receipt is reclaimed
// only once its claim goes stale, so the sweep never steals work from a
// live in-flight worker.
//
// The receipt-level UpdatedAt is the claim timestamp: it is refreshed on
// every Update, so a routing receipt whose most recent claim/finalize is
// fresher than the window is a live claim and skipped; a stale one (a
// crashed worker) is reclaimed. A routing receipt with no recorded
// UpdatedAt is reclaimed immediately.
func sweepEligible(r *IngressReceipt, now time.Time, window time.Duration) bool {
	if isTerminalStatus(r.Status) {
		return false
	}
	// S7 backoff gate: a correlation park scheduled for a future RecoveryNextAt
	// is not yet due (an adapter restart neither bypasses the backoff nor bumps
	// the attempt count — recoverPending consults recoveryDue too).
	if !recoveryDue(r, now) {
		return false
	}
	if r.Status != ingressStatusRouting {
		return true
	}
	// A session-existence guard hold commits the receipt back to routing with a
	// fresh UpdatedAt, but it is NOT a live in-flight claim — the worker
	// deliberately deferred and released. The spec promises the 60s sweep
	// re-checks such holds, so exempt them from the 5-minute stale-reclaim gate
	// (applies to room and DM deliveries equally). The in-process single-flight
	// claim + generation-checked Update still bound concurrent redrives.
	if guardHeldPending(r) {
		return true
	}
	if r.UpdatedAt.IsZero() {
		return true
	}
	return now.Sub(r.UpdatedAt) >= window
}

// guardHeldPending reports whether any of a receipt's targets is pending behind
// a session-existence guard hold (session_missing / session_ambiguous). Such a
// receipt sits in status routing but is not being actively worked — the last
// worker held it for the sweep to re-check on the 60s cadence.
func guardHeldPending(r *IngressReceipt) bool {
	for _, td := range r.Targets {
		if td.Status != companyTargetPending {
			continue
		}
		if td.Detail == companyDetailSessionMissing || td.Detail == companyDetailSessionAmbiguous ||
			td.Detail == companyDetailMaterializing {
			return true
		}
	}
	return false
}

// reloadOnSIGHUP stages/commits both company stores independently of the
// six-registry atomic set. A stale or invalid file retains its own
// last-known-good snapshot (handled inside StageReload) and never blocks
// the six. Directory is reloaded first because bindings validity depends
// on it. A nil gateway is a no-op.
func (g *companyGateway) reloadOnSIGHUP() {
	if g == nil {
		return
	}
	_ = g.dirStore.StageReload(g.cfg.companyDirectoryPath)
	_ = g.bindStore.StageReload(g.cfg.companyBindingsPath, g.dirStore.Snapshot())
	// Phase 4 registries reload on the same SIGHUP, after the directory (both
	// validate against its snapshot). Each keeps its own last-known-good on a
	// bad file (StageReload contract).
	if g.dmBindStore != nil {
		_ = g.dmBindStore.StageReload(g.cfg.companyDMBindingsPath, g.dirStore.Snapshot())
	}
	if g.agentApps != nil {
		_ = g.agentApps.StageReload(g.cfg.companyAgentAppsPath, g.dirStore.Snapshot())
	}
	log.Printf("company registries reloaded: directory_loaded=%v bindings_loaded=%v dm_bindings_loaded=%v agent_apps=%d",
		g.dirStore.Snapshot() != nil, g.bindStore.Snapshot() != nil,
		g.dmBindStore.Snapshot() != nil, g.agentApps.Snapshot().Len())
}

// healthzDetail returns the company status lines appended to /healthz: the
// barrier state, receipt-store readiness plus its last construction error
// (the degraded-mode paging hook), the write-failure counter, the
// delivery-failure counter, and directory/bindings snapshot state. This is
// the sole status surface for the company path — there is no separate
// gateway status payload endpoint in this pack.
func (g *companyGateway) healthzDetail() string {
	if g == nil {
		return ""
	}
	store := g.store()
	storeReady := store != nil
	var writeFailures uint64
	var bodyDigestMismatch uint64
	if store != nil {
		writeFailures = store.WriteFailures()
		bodyDigestMismatch = store.BodyDigestMismatches()
	}
	storeErr := ""
	if p := g.storeErr.Load(); p != nil {
		storeErr = *p
	}
	var dm dmFamilyReceiptCounts
	if p := g.dmStatusCounts.Load(); p != nil {
		dm = *p
	}
	var body bodyIntegrityCounts
	if p := g.bodyIntegrity.Load(); p != nil {
		body = *p
	}
	// registered_agent_apps count + directory-join warnings (Phase 4). Warnings
	// are recomputed against the live directory snapshot each call so an
	// operator sees them clear once the directory is fixed (no restart needed).
	apps := g.agentApps.Snapshot()
	joinWarnings := apps.JoinWarnings(g.dirStore.Snapshot())
	return fmt.Sprintf(
		"company_barrier_ready=%v\ncompany_store_ready=%v\ncompany_store_error=%q\ncompany_write_failures=%d\ncompany_delivery_failures=%d\ncompany_target_reresolved=%d\ncompany_materialize_requests=%d\ncompany_stale_posting_intents=%d\ncompany_correlation_parked=%d\ncompany_directory_loaded=%v\ncompany_bindings_loaded=%v\ncompany_dm_bindings_loaded=%v\nregistered_agent_apps=%d\nregistered_agent_apps_join_warnings=%d\ncompany_dm_sig_reject=%d\ncompany_dm_token_missing=%d\ncompany_dm_receipts_received=%d\ncompany_dm_receipts_routing=%d\ncompany_dm_receipts_delivered=%d\ncompany_dm_receipts_no_delivery=%d\ncompany_dm_receipts_failed=%d\ncompany_mpim_receipts_received=%d\ncompany_mpim_receipts_routing=%d\ncompany_mpim_receipts_delivered=%d\ncompany_mpim_receipts_no_delivery=%d\ncompany_mpim_receipts_failed=%d\ncompany_body_missing=%d\ncompany_bodies_redacted=%d\ncompany_body_digest_mismatch=%d\n",
		g.barrier.Load(),
		storeReady,
		storeErr,
		writeFailures,
		g.deliveryFailures.Load(),
		g.targetReresolved.Load(),
		g.materializeRequests.Load(),
		g.stalePostingIntents.Load(),
		g.companyCorrelationParked.Load(),
		g.dirStore.Snapshot() != nil,
		g.bindStore.Snapshot() != nil,
		g.dmBindStore.Snapshot() != nil,
		apps.Len(),
		len(joinWarnings),
		g.dmSigRejects.Load(),
		g.dmTokenMissing.Load(),
		dm.DM.Received,
		dm.DM.Routing,
		dm.DM.Delivered,
		dm.DM.NoDelivery,
		dm.DM.Failed,
		dm.Mpim.Received,
		dm.Mpim.Routing,
		dm.Mpim.Delivered,
		dm.Mpim.NoDelivery,
		dm.Mpim.Failed,
		body.Missing,
		body.Redacted,
		bodyDigestMismatch,
	)
}
