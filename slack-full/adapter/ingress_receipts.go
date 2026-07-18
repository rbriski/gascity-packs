package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// IngressReceiptStore is the durable ingress ledger for company-room
// events (Slack company-rooms Phase 1b). Every admissible company-room
// POST becomes exactly one receipt on disk before the transport is
// acknowledged; the file system is the deduplication authority across
// Slack redeliveries and adapter restarts.
//
// Durability contract:
//
//   - Admit is the admission linearization point and is claim-and-content
//     atomic. The complete receipt (including the full inner Slack event
//     and outer identifiers, which routing and crash replay both require)
//     is written to a temp file, fsynced, then hard-linked (os.Link) to
//     its origin-keyed final name. os.Link is atomic and fails with
//     EEXIST if the name already exists, so the first writer wins and a
//     redelivery observes the existing receipt. There is no window in
//     which a claim (the final name) exists without its content (the
//     linked inode already holds the fully-fsynced bytes).
//   - Update is a generation-checked atomic rewrite (temp + rename + fsync
//     of file and directory). A lost race is detected via the generation
//     counter (ErrStale) rather than silently overwritten.
//   - Files are 0600 inside a 0700 directory. Filenames derive from the
//     origin through safeStorageID, which hashes long or hostile
//     components so a crafted ts/channel can neither escape the directory
//     nor produce an unbounded name.
//
// A crash between the temp write and the link, or between a rename's
// components, leaves at most a stray "ingress-*.tmp" orphan — never a
// half-written receipt. The orphan is harmless (it is ignored by every
// scan) and swept by the adapter's tmp janitor.
type IngressReceiptStore struct {
	dir string

	// mu serializes all file operations so that Update's read-modify-write
	// is atomic within the process. Cross-process safety rests on os.Link
	// atomicity (Admit) and the generation counter (Update); the delivery
	// workers additionally hold an in-process single-flight claim per
	// receipt id (Phase 1d).
	mu sync.Mutex

	// writeFailures counts failed Admit/Update persistence attempts. It is
	// read without the lock by the gateway status payload and /healthz, so
	// it is an atomic counter rather than a mutex-guarded field.
	writeFailures atomic.Uint64
}

// Receipt status values. Non-terminal receipts (received, routing) are
// still in flight and are returned by Pending; terminal receipts
// (delivered, no_delivery, failed) are the dedup memory swept after
// retention.
const (
	ingressStatusReceived   = "received"
	ingressStatusRouting    = "routing"
	ingressStatusDelivered  = "delivered"
	ingressStatusNoDelivery = "no_delivery"
	ingressStatusFailed     = "failed"
)

// ingressRetentionFloor is the minimum accepted Sweep retention. Terminal
// receipts are the dedup memory for Slack's Delayed Events redelivery
// horizon (hourly retries for 24h), so retention below 24h is rejected —
// sweeping sooner would let a late redelivery re-admit an already-handled
// event.
const ingressRetentionFloor = 24 * time.Hour

// maxIngressReceiptBytes caps the on-disk receipt size accepted at read.
// A Slack event envelope is a few KiB; 4 MiB is several orders of
// magnitude over that and a larger file is presumed corrupt.
const maxIngressReceiptBytes = 4 << 20 // 4 MiB

// maxSafeComponentLen bounds a readable origin component. A longer value
// is hashed rather than embedded verbatim so a hostile ts/channel cannot
// produce an unbounded filename.
const maxSafeComponentLen = 64

// ErrStale is returned by Update when the on-disk generation differs from
// the caller's receipt generation. The caller re-reads, merges, and
// retries — a lost race is never silently overwritten.
var ErrStale = errors.New("ingress receipts: stale generation")

// ReceiptOrigin is the canonical dedup key for a company-room event:
// (team_id, channel_id, ts). ts is unique per channel; the observing app
// is never a key input.
type ReceiptOrigin struct {
	TeamID    string `json:"team_id"`
	ChannelID string `json:"channel_id"`
	TS        string `json:"ts"`
}

// TargetDelivery records the per-session delivery state of a receipt.
type TargetDelivery struct {
	Session string `json:"session"`
	// City optionally targets a session in a different gc city than the
	// adapter's own (city-qualified binding); empty = the adapter's city.
	City string `json:"city,omitempty"`
	// Kind is the frozen wake kind: "ambient" | "targeted" (human legs) or
	// "peer_delegation" | "peer_result" | "peer_input" (company-bot legs,
	// Phase 2c). Only peer_delegation / peer_result carry a DelegationKey.
	Kind           string    `json:"kind"`
	Status         string    `json:"status"`          // "pending" | "delivered" | "failed"
	IdempotencyKey string    `json:"idempotency_key"` // ingress:<id>:target:<session>
	Attempts       int       `json:"attempts"`
	UpdatedAt      time.Time `json:"updated_at"`
	Detail         string    `json:"detail,omitempty"`
	// Agent is the directory agent name for this target, recorded so the
	// current-turn pointer can be rewritten on redrive without recomputing
	// the route. Empty for a failed-unbound record.
	Agent string `json:"agent,omitempty"`
	// DelegationKey is the delegations-registry filename this peer turn
	// correlates to (set for peer_delegation / peer_result legs only; empty
	// for peer_input and the human ambient/targeted legs).
	DelegationKey string `json:"delegation_key,omitempty"`
}

// IngressReceipt is the durable ingress record. It retains the complete
// inner Slack event so async routing and post-crash replay never depend
// on re-fetching from Slack.
type IngressReceipt struct {
	ID          string        `json:"id"` // "in-" + sanitized origin
	Generation  int64         `json:"generation"`
	Origin      ReceiptOrigin `json:"origin"`
	EventID     string        `json:"event_id"`
	APIAppID    string        `json:"api_app_id"`
	RetryNum    int           `json:"retry_num"`
	RetryReason string        `json:"retry_reason,omitempty"`
	ReceivedAt  time.Time     `json:"received_at"`
	// UpdatedAt is refreshed on Admit and on every Update. The delivery
	// sweep uses it as the claim timestamp for the stale-reclaim window: a
	// "routing" receipt whose UpdatedAt is fresher than the window is a live
	// claim and skipped; a stale one is reclaimed.
	UpdatedAt time.Time `json:"updated_at"`
	Status    string    `json:"status"` // "received" | "routing" | "delivered" | "no_delivery" | "failed"
	// Event is the COMPLETE inner Slack event object as received —
	// routing (text/blocks/thread_ts) and crash replay depend on it.
	Event   json.RawMessage           `json:"event"`
	Targets map[string]TargetDelivery `json:"targets,omitempty"`
	Reason  string                    `json:"reason,omitempty"` // parked/no_delivery/failed detail
	// Hydration is the frozen context bundle (verified human root +
	// bounded untrusted excerpt) fetched ONCE at first delivery so redrives
	// re-render byte-identical reminders under the same Idempotency-Key
	// (Phase 2c). Absent until the first delivery attempt computes it.
	Hydration json.RawMessage `json:"hydration,omitempty"`
	// Synthesis is the frozen snapshot bytes for a peer_result receipt,
	// copied from the claimed record in the same routing commit that freezes
	// targets, so redrives re-render byte-identical reminder synthesis fields
	// even if the record is later pruned (the same frozen-bytes discipline as
	// Hydration). Absent on non-peer_result receipts (Phase 3a).
	Synthesis json.RawMessage `json:"synthesis,omitempty"`
	// RecoveryAttempts / RecoveryNextAt / RecoveryReason track the S7
	// correlation-park backoff (rule 17 parity, Phase 3b): a
	// correlation_pending or ambiguous_pending_delegations park counts one
	// attempt only when a redrive re-ran resolveResultWake and found the
	// posting intent still in flight, and schedules the next eligible pass at
	// min(60s*2^(n-1), 15min). NOTE omitzero on RecoveryNextAt, not omitempty:
	// encoding/json's omitempty never elides a struct, so a zero 0001-01-01
	// timestamp would otherwise pollute every receipt's cross-language wire
	// shape (go.mod is go >= 1.24, omitzero available).
	RecoveryAttempts int       `json:"recovery_attempts,omitempty"`
	RecoveryNextAt   time.Time `json:"recovery_next_at,omitzero"`
	RecoveryReason   string    `json:"recovery_reason,omitempty"`
	// AckState is the visible-ack cursor (Phase 3b, config-gated):
	// "" | "eyes" | "done" | "degraded". Cosmetic — the durable receipt, not
	// the emoji, stays authoritative, so an ack failure never changes status.
	AckState string `json:"ack_state,omitempty"`
}

// NewIngressReceiptStore opens (creating if needed) the receipt directory
// rooted at dir with 0700 permissions. dir must be non-empty; the caller
// resolves it (Phase 1d) from GC_CITY_PATH / SLACK_COMPANY_INGRESS_DIR.
func NewIngressReceiptStore(dir string) (*IngressReceiptStore, error) {
	if strings.TrimSpace(dir) == "" {
		return nil, errors.New("ingress receipts: store dir required")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("ingress receipts: mkdir %q: %w", dir, err)
	}
	// Reject a symlinked store dir before we operate through it. A
	// same-UID attacker or /tmp squatter who points chat-ingress at another
	// tree would otherwise redirect every receipt write; MkdirAll/Chmod
	// both follow the link. This is the confined-open convention the six
	// atomic registries enforce (openRegistryFile, gc-cby.38), applied to
	// the most security-sensitive durable state in the pack.
	info, err := os.Lstat(dir)
	if err != nil {
		return nil, fmt.Errorf("ingress receipts: lstat %q: %w", dir, err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return nil, fmt.Errorf("ingress receipts: store dir %q is a symlink; refusing to open", dir)
	}
	// Tighten perms in case the directory pre-existed with looser bits.
	if err := os.Chmod(dir, 0o700); err != nil {
		return nil, fmt.Errorf("ingress receipts: chmod %q: %w", dir, err)
	}
	return &IngressReceiptStore{dir: dir}, nil
}

// Admit is the linearization point, claim-and-content atomic. The full
// receipt is written to a temp file in the store directory, fsynced, then
// hard-linked (os.Link) to its final origin-keyed name; the directory is
// fsynced and the temp name removed. os.Link failing with EEXIST means a
// receipt for this origin already exists (a duplicate / redelivery): the
// existing receipt is read and returned with created=false. An existing
// but unparseable receipt file is quarantined (*.corrupt) and the link
// retried once; a persistent failure returns err (the caller answers
// 503). There is no state in which a claim exists without its content.
func (s *IngressReceiptStore) Admit(r *IngressReceipt) (created bool, existing *IngressReceipt, err error) {
	if r == nil {
		return false, nil, errors.New("ingress receipts: nil receipt")
	}
	if err := validateOrigin(r.Origin); err != nil {
		return false, nil, err
	}
	r.ID = receiptID(r.Origin)
	if r.Status == "" {
		r.Status = ingressStatusReceived
	}
	if r.Generation <= 0 {
		r.Generation = 1
	}
	if r.ReceivedAt.IsZero() {
		r.ReceivedAt = time.Now().UTC()
	}
	if r.UpdatedAt.IsZero() {
		r.UpdatedAt = r.ReceivedAt
	}
	normalizeEvent(r)

	data, err := marshalReceipt(r)
	if err != nil {
		return false, nil, err
	}
	finalPath := s.pathForID(r.ID)

	s.mu.Lock()
	defer s.mu.Unlock()

	quarantined := false
	for {
		tmp, werr := s.writeTempReceipt(data)
		if werr != nil {
			s.writeFailures.Add(1)
			return false, nil, werr
		}
		linkErr := os.Link(tmp, finalPath)
		if linkErr == nil {
			// The claim (final name) now points at the fully-fsynced
			// inode. Durably record the new directory entry before
			// removing our temp alias.
			if serr := fsyncDir(s.dir); serr != nil {
				_ = os.Remove(tmp)
				s.writeFailures.Add(1)
				return false, nil, fmt.Errorf("ingress receipts: fsync dir after link: %w", serr)
			}
			_ = os.Remove(tmp)
			return true, nil, nil
		}
		_ = os.Remove(tmp)
		if !errors.Is(linkErr, os.ErrExist) {
			s.writeFailures.Add(1)
			return false, nil, fmt.Errorf("ingress receipts: link %q: %w", finalPath, linkErr)
		}
		// EEXIST: a receipt already occupies the final name.
		existingR, rerr := s.readReceiptFile(finalPath)
		if rerr == nil {
			return false, existingR, nil
		}
		if errors.Is(rerr, os.ErrNotExist) {
			// The final vanished between our link attempt and the read
			// (a concurrent removal). Retry the link.
			continue
		}
		// Existing receipt is unparseable. Quarantine once and retry.
		if quarantined {
			s.writeFailures.Add(1)
			return false, nil, fmt.Errorf("ingress receipts: persistent corrupt receipt %q: %w", finalPath, rerr)
		}
		if qerr := s.quarantine(finalPath); qerr != nil {
			s.writeFailures.Add(1)
			return false, nil, fmt.Errorf("ingress receipts: quarantine corrupt receipt %q: %w", finalPath, qerr)
		}
		quarantined = true
	}
}

// Update performs a generation-checked atomic rewrite (temp + rename +
// fsync of file and directory — same durability as Admit). If the on-disk
// generation differs from r.Generation, ErrStale is returned; the caller
// re-reads and merges. On success r.Generation is advanced to the value
// written (on-disk generation + 1).
func (s *IngressReceiptStore) Update(r *IngressReceipt) error {
	if r == nil {
		return errors.New("ingress receipts: nil receipt")
	}
	if err := validateOrigin(r.Origin); err != nil {
		return err
	}
	r.ID = receiptID(r.Origin)
	if r.Status == "" {
		r.Status = ingressStatusReceived
	}
	normalizeEvent(r)

	finalPath := s.pathForID(r.ID)

	s.mu.Lock()
	defer s.mu.Unlock()

	onDisk, err := s.readReceiptFile(finalPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("ingress receipts: update missing receipt %q: %w", r.ID, err)
		}
		s.writeFailures.Add(1)
		return fmt.Errorf("ingress receipts: read for update %q: %w", finalPath, err)
	}
	if onDisk.Generation != r.Generation {
		return ErrStale
	}
	r.Generation = onDisk.Generation + 1
	r.UpdatedAt = time.Now().UTC()
	data, err := marshalReceipt(r)
	if err != nil {
		return err
	}
	if err := s.writeReceiptAtomic(finalPath, data); err != nil {
		s.writeFailures.Add(1)
		return err
	}
	return nil
}

// Get returns the receipt for origin, or (nil, nil) if none exists.
func (s *IngressReceiptStore) Get(origin ReceiptOrigin) (*IngressReceipt, error) {
	if err := validateOrigin(origin); err != nil {
		return nil, err
	}
	finalPath := s.pathForID(receiptID(origin))

	s.mu.Lock()
	defer s.mu.Unlock()

	r, err := s.readReceiptFile(finalPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	return r, nil
}

// GetByID reads one receipt by its receipt id (the origin-keyed
// "in-…" filename stem). Returns (nil, nil) when the receipt does not exist
// — a terminal receipt swept past retention is gone, which the redrive
// endpoint surfaces as 404. A symlinked/corrupt file is returned as an error
// (never quarantined here — this is a targeted read, not a scan).
func (s *IngressReceiptStore) GetByID(id string) (*IngressReceipt, error) {
	// Defense in depth: never join an id that is not the receipt-id shape into a
	// filesystem path. GetByID is the one id ingress that skips origin-derivation
	// (and thus safeStorageID sanitization), so a hostile id (path separator, NUL,
	// traversal) must be rejected before pathForID. The redrive handler validates
	// the same shape up front and returns 400; this guard covers any other caller.
	if !isReceiptID(id) {
		return nil, fmt.Errorf("ingress receipts: invalid receipt id %q", id)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.readReceiptFile(s.pathForID(id))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	return r, nil
}

// List returns every receipt in the store (terminal and non-terminal),
// ordered by (ReceivedAt, Origin.TS) — the operator-listing scan behind
// GET /internal/company/receipts. A corrupt scan entry is quarantined and
// skipped exactly like Pending; only a directory-listing failure is fatal.
func (s *IngressReceiptStore) List() ([]*IngressReceipt, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	paths, err := s.receiptFiles()
	if err != nil {
		return nil, err
	}
	var out []*IngressReceipt
	for _, p := range paths {
		r, rerr := s.readReceiptFile(p)
		if rerr != nil {
			if errors.Is(rerr, os.ErrNotExist) {
				continue // raced removal
			}
			s.quarantineNonFatal(p, rerr)
			continue
		}
		out = append(out, r)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if !out[i].ReceivedAt.Equal(out[j].ReceivedAt) {
			return out[i].ReceivedAt.Before(out[j].ReceivedAt)
		}
		return out[i].Origin.TS < out[j].Origin.TS
	})
	return out, nil
}

// Pending returns non-terminal receipts ordered by (ReceivedAt,
// Origin.TS). A corrupt receipt file encountered during the scan is
// quarantined (*.corrupt) and skipped — a single bad file never fails the
// scan. Only a directory-listing failure is fatal.
func (s *IngressReceiptStore) Pending() ([]*IngressReceipt, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	paths, err := s.receiptFiles()
	if err != nil {
		return nil, err
	}
	var pending []*IngressReceipt
	for _, p := range paths {
		r, rerr := s.readReceiptFile(p)
		if rerr != nil {
			if errors.Is(rerr, os.ErrNotExist) {
				continue // raced removal
			}
			s.quarantineNonFatal(p, rerr)
			continue
		}
		if isTerminalStatus(r.Status) {
			continue
		}
		pending = append(pending, r)
	}
	sort.SliceStable(pending, func(i, j int) bool {
		if !pending[i].ReceivedAt.Equal(pending[j].ReceivedAt) {
			return pending[i].ReceivedAt.Before(pending[j].ReceivedAt)
		}
		return pending[i].Origin.TS < pending[j].Origin.TS
	})
	return pending, nil
}

// Sweep removes terminal receipts whose ReceivedAt is older than
// retention and returns the count removed. Retention below 24h is
// rejected (terminal receipts are the dedup memory for the Delayed Events
// horizon). Non-terminal receipts are never touched; corrupt scan entries
// are quarantined rather than fatal.
func (s *IngressReceiptStore) Sweep(retention time.Duration) (removed int, err error) {
	if retention < ingressRetentionFloor {
		return 0, fmt.Errorf("ingress receipts: retention %s below %s floor", retention, ingressRetentionFloor)
	}
	cutoff := time.Now().Add(-retention)

	s.mu.Lock()
	defer s.mu.Unlock()

	paths, err := s.receiptFiles()
	if err != nil {
		return 0, err
	}
	removedAny := false
	for _, p := range paths {
		r, rerr := s.readReceiptFile(p)
		if rerr != nil {
			if errors.Is(rerr, os.ErrNotExist) {
				continue
			}
			s.quarantineNonFatal(p, rerr)
			continue
		}
		if !isTerminalStatus(r.Status) {
			continue // never sweep in-flight work
		}
		if !r.ReceivedAt.Before(cutoff) {
			continue // still within retention
		}
		if derr := os.Remove(p); derr != nil {
			if errors.Is(derr, os.ErrNotExist) {
				continue
			}
			log.Printf("WARN: ingress receipts: sweep remove %q: %v", p, derr)
			continue
		}
		removed++
		removedAny = true
	}
	if removedAny {
		// Durably record the removed directory entries.
		if serr := fsyncDir(s.dir); serr != nil {
			log.Printf("WARN: ingress receipts: fsync dir after sweep: %v", serr)
		}
	}
	return removed, nil
}

// SweepAndPending performs the periodic sweep in a single directory scan:
// it removes terminal receipts older than retention, returns the surviving
// non-terminal receipts ordered by (ReceivedAt, Origin.TS), AND returns the
// terminal-within-retention receipts whose visible-ack cursor is stranded on
// "eyes" or "warned" (healNeeded). Folding the ack-heal candidates into this one
// scan avoids a second full-store decode pass per tick under the store mutex
// Admit contends for on the HTTP hot path. Retention below the floor is rejected
// exactly like Sweep; corrupt scan entries are quarantined rather than fatal.
//
// A stranded cursor is one whose terminal reaction (✅/⚠️/remove) never landed
// (rate-limited, or a crash between the finalize commit and the ack commit).
// "warned" is the failed-path intermediate where the threaded reply is already
// posted and only the ⚠️ reaction remains, so healing re-applies the reaction
// WITHOUT re-posting the reply. healNeeded is always collected (cheap on the
// already-decoded receipt); the caller applies it only when acks are enabled.
func (s *IngressReceiptStore) SweepAndPending(retention time.Duration) (pending, healNeeded []*IngressReceipt, removed int, err error) {
	if retention < ingressRetentionFloor {
		return nil, nil, 0, fmt.Errorf("ingress receipts: retention %s below %s floor", retention, ingressRetentionFloor)
	}
	cutoff := time.Now().Add(-retention)

	s.mu.Lock()
	defer s.mu.Unlock()

	paths, err := s.receiptFiles()
	if err != nil {
		return nil, nil, 0, err
	}
	removedAny := false
	for _, p := range paths {
		r, rerr := s.readReceiptFile(p)
		if rerr != nil {
			if errors.Is(rerr, os.ErrNotExist) {
				continue // raced removal
			}
			s.quarantineNonFatal(p, rerr)
			continue
		}
		if !isTerminalStatus(r.Status) {
			pending = append(pending, r)
			continue // never sweep in-flight work
		}
		if !r.ReceivedAt.Before(cutoff) {
			// Terminal but within retention: not swept. Collect a stranded
			// visible-ack cursor for in-pass healing (fold of the former
			// TerminalAcksNeedingHeal second scan).
			if r.AckState == ackStateEyes || r.AckState == ackStateWarned {
				healNeeded = append(healNeeded, r)
			}
			continue
		}
		if derr := os.Remove(p); derr != nil {
			if errors.Is(derr, os.ErrNotExist) {
				continue
			}
			log.Printf("WARN: ingress receipts: sweep remove %q: %v", p, derr)
			continue
		}
		removed++
		removedAny = true
	}
	if removedAny {
		if serr := fsyncDir(s.dir); serr != nil {
			log.Printf("WARN: ingress receipts: fsync dir after sweep: %v", serr)
		}
	}
	sort.SliceStable(pending, func(i, j int) bool {
		if !pending[i].ReceivedAt.Equal(pending[j].ReceivedAt) {
			return pending[i].ReceivedAt.Before(pending[j].ReceivedAt)
		}
		return pending[i].Origin.TS < pending[j].Origin.TS
	})
	return pending, healNeeded, removed, nil
}

// WriteFailures returns the monotonic count of failed Admit/Update
// persistence attempts, surfaced in the gateway status payload and
// /healthz detail as the receipt-store outage paging hook.
func (s *IngressReceiptStore) WriteFailures() uint64 {
	return s.writeFailures.Load()
}

// pathForID returns the on-disk receipt path for a receipt id.
func (s *IngressReceiptStore) pathForID(id string) string {
	return filepath.Join(s.dir, id+".json")
}

// receiptFiles lists the receipt files (*.json) in the store directory,
// skipping temp (*.tmp) and quarantined (*.corrupt) files. A missing
// directory is treated as empty.
func (s *IngressReceiptStore) receiptFiles() ([]string, error) {
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("ingress receipts: scan %q: %w", s.dir, err)
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		if !strings.HasSuffix(name, ".json") {
			continue
		}
		out = append(out, filepath.Join(s.dir, name))
	}
	return out, nil
}

// writeTempReceipt writes data to a fresh 0600 temp file in the store
// directory, fsyncs it, and returns the temp path. The caller either
// links it (Admit) or renames it (Update); on any failure the temp is
// removed before returning.
func (s *IngressReceiptStore) writeTempReceipt(data []byte) (string, error) {
	f, err := os.CreateTemp(s.dir, "ingress-*.tmp")
	if err != nil {
		return "", fmt.Errorf("ingress receipts: create temp in %q: %w", s.dir, err)
	}
	tmp := f.Name()
	cleanup := func() { _ = os.Remove(tmp) }
	if err := f.Chmod(0o600); err != nil {
		_ = f.Close()
		cleanup()
		return "", fmt.Errorf("ingress receipts: chmod %q: %w", tmp, err)
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		cleanup()
		return "", fmt.Errorf("ingress receipts: write %q: %w", tmp, err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		cleanup()
		return "", fmt.Errorf("ingress receipts: fsync %q: %w", tmp, err)
	}
	if err := f.Close(); err != nil {
		cleanup()
		return "", fmt.Errorf("ingress receipts: close %q: %w", tmp, err)
	}
	return tmp, nil
}

// writeReceiptAtomic rewrites path via temp + rename with an fsync of both
// the file and the directory. Used by Update, which has already verified
// the receipt exists.
func (s *IngressReceiptStore) writeReceiptAtomic(path string, data []byte) error {
	tmp, err := s.writeTempReceipt(data)
	if err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("ingress receipts: rename %q -> %q: %w", tmp, path, err)
	}
	if err := fsyncDir(s.dir); err != nil {
		return fmt.Errorf("ingress receipts: fsync dir after rename: %w", err)
	}
	return nil
}

// readReceiptFile reads and decodes a receipt file. A missing file
// surfaces os.ErrNotExist; an oversized or malformed file surfaces a
// decode error the caller treats as corruption.
func (s *IngressReceiptStore) readReceiptFile(path string) (*IngressReceipt, error) {
	// O_NOFOLLOW rejects a receipt file swapped for a symlink (same-UID
	// attacker / /tmp squatter) so a hostile link cannot redirect the
	// durable-ledger read outside the store dir. A symlinked receipt trips
	// ELOOP here and is treated as corrupt (quarantined by the scan callers)
	// rather than followed. Parity with openRegistryFile's symlink guard.
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		// Wrap so callers keep using errors.Is(err, os.ErrNotExist): a bare
		// syscall.ENOENT already satisfies it, and *PathError preserves that.
		return nil, &os.PathError{Op: "open", Path: path, Err: err}
	}
	f := os.NewFile(uintptr(fd), path)
	defer func() { _ = f.Close() }()
	data, err := io.ReadAll(io.LimitReader(f, maxIngressReceiptBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read %q: %w", path, err)
	}
	if int64(len(data)) > maxIngressReceiptBytes {
		return nil, fmt.Errorf("receipt %q exceeds %d bytes", path, maxIngressReceiptBytes)
	}
	var r IngressReceipt
	if err := json.Unmarshal(data, &r); err != nil {
		return nil, fmt.Errorf("decode %q: %w", path, err)
	}
	return &r, nil
}

// quarantine renames a corrupt receipt file aside to a unique *.corrupt
// name and fsyncs the directory. The random token keeps repeated
// quarantines of the same origin from clobbering earlier forensic copies.
func (s *IngressReceiptStore) quarantine(path string) error {
	target := path + "-" + randomHexToken() + ".corrupt"
	if err := os.Rename(path, target); err != nil {
		return fmt.Errorf("rename %q -> %q: %w", path, target, err)
	}
	if err := fsyncDir(s.dir); err != nil {
		return fmt.Errorf("fsync dir after quarantine: %w", err)
	}
	log.Printf("WARN: ingress receipts: quarantined corrupt receipt %q -> %q", path, target)
	return nil
}

// quarantineNonFatal quarantines a corrupt scan entry, logging rather than
// propagating any failure — a single bad file must never fail a scan.
func (s *IngressReceiptStore) quarantineNonFatal(path string, cause error) {
	if qerr := s.quarantine(path); qerr != nil {
		log.Printf("WARN: ingress receipts: quarantine %q (corrupt: %v) failed: %v", path, cause, qerr)
	}
}

// validateOrigin rejects an origin missing any keyed component. Unkeyable
// events are dropped before Admit (Phase 1d), so this is a defensive
// guard rather than a routine path.
func validateOrigin(o ReceiptOrigin) error {
	if o.TeamID == "" || o.ChannelID == "" || o.TS == "" {
		return fmt.Errorf("ingress receipts: origin requires team/channel/ts, got %+v", o)
	}
	return nil
}

// normalizeEvent guarantees the Event field is valid JSON so marshaling a
// receipt with an absent inner event cannot error.
func normalizeEvent(r *IngressReceipt) {
	if len(r.Event) == 0 {
		r.Event = json.RawMessage("null")
	}
}

// marshalReceipt encodes a receipt to indented JSON.
func marshalReceipt(r *IngressReceipt) ([]byte, error) {
	data, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("ingress receipts: marshal %q: %w", r.ID, err)
	}
	return data, nil
}

// isTerminalStatus reports whether a receipt status is terminal
// (delivered / no_delivery / failed). Non-terminal receipts are returned
// by Pending and never swept.
func isTerminalStatus(status string) bool {
	switch status {
	case ingressStatusDelivered, ingressStatusNoDelivery, ingressStatusFailed:
		return true
	default:
		return false
	}
}

// receiptID derives the origin-keyed receipt id. The readable prefix is
// built from safeStorageID-sanitized components (hostile/long components
// hashed); a short digest over the exact (team, channel, ts) tuple —
// separated by NUL so components can never be confused — is appended so
// two distinct origins can never collide even if their sanitized readable
// forms would.
func receiptID(o ReceiptOrigin) string {
	readable := safeStorageID(o.TeamID, "team") + "-" +
		safeStorageID(o.ChannelID, "chan") + "-" +
		safeStorageID(o.TS, "ts")
	sum := sha256.Sum256([]byte(o.TeamID + "\x00" + o.ChannelID + "\x00" + o.TS))
	return "in-" + readable + "-" + hex.EncodeToString(sum[:])[:12]
}

// isReceiptID reports whether id matches the exact shape receiptID produces:
// "in-" followed by one or more bytes from [A-Za-z0-9._-] (the pattern
// ^in-[A-Za-z0-9._-]+$). Every generated id passes — the readable prefix is
// built from safeStorageID components (that same character set) and a hex digest
// — while any id carrying a path separator, NUL, or other hostile byte is
// rejected before it can be joined into a store path.
func isReceiptID(id string) bool {
	if !strings.HasPrefix(id, "in-") || len(id) <= len("in-") {
		return false
	}
	for i := 0; i < len(id); i++ {
		ch := id[i]
		switch {
		case ch >= 'a' && ch <= 'z':
		case ch >= 'A' && ch <= 'Z':
		case ch >= '0' && ch <= '9':
		case ch == '.' || ch == '_' || ch == '-':
		default:
			return false
		}
	}
	return true
}

// safeStorageID returns value unchanged when it is a short, filename-safe
// token; otherwise it returns "<prefix>-<sha256[:24]>". Mirrors the
// intake packs' safe_storage_id sanitizer (hash long/hostile components).
func safeStorageID(value, prefix string) string {
	value = strings.TrimSpace(value)
	if value != "" && len(value) <= maxSafeComponentLen && isSafeStorageValue(value) {
		return value
	}
	sum := sha256.Sum256([]byte(value))
	return prefix + "-" + hex.EncodeToString(sum[:])[:24]
}

// isSafeStorageValue reports whether v is composed only of ASCII
// alphanumerics and the separators '-', '_', '.', with no path-traversal
// sequence or leading dot. Anything else is considered hostile and hashed.
func isSafeStorageValue(v string) bool {
	if strings.Contains(v, "..") || strings.HasPrefix(v, ".") {
		return false
	}
	for i := 0; i < len(v); i++ {
		ch := v[i]
		switch {
		case ch >= 'a' && ch <= 'z':
		case ch >= 'A' && ch <= 'Z':
		case ch >= '0' && ch <= '9':
		case ch == '-' || ch == '_' || ch == '.':
		default:
			return false
		}
	}
	return true
}

// fsyncDir fsyncs a directory so a rename/link/remove of one of its
// entries is durable across a crash.
func fsyncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	serr := d.Sync()
	cerr := d.Close()
	if serr != nil {
		return serr
	}
	return cerr
}

// randomHexToken returns 16 hex chars of randomness for quarantine names.
// On the (practically impossible) crypto/rand failure it falls back to a
// nanosecond timestamp; quarantine naming is best-effort forensics, not a
// correctness primitive.
func randomHexToken() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b[:])
}
