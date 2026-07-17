package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func newTestReceiptStore(t *testing.T) (*IngressReceiptStore, string) {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "chat-ingress")
	s, err := NewIngressReceiptStore(dir)
	if err != nil {
		t.Fatalf("NewIngressReceiptStore: %v", err)
	}
	return s, dir
}

func sampleReceipt(team, channel, ts string) *IngressReceipt {
	return &IngressReceipt{
		Origin:     ReceiptOrigin{TeamID: team, ChannelID: channel, TS: ts},
		EventID:    "Ev" + ts,
		APIAppID:   "A0SWITCH",
		ReceivedAt: time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC),
		Event:      json.RawMessage(`{"type":"message","channel":"` + channel + `","ts":"` + ts + `","text":"hi"}`),
	}
}

func countCorruptFiles(t *testing.T, dir string) int {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("ReadDir %q: %v", dir, err)
	}
	n := 0
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".corrupt") {
			n++
		}
	}
	return n
}

// TestIngressReceiptAdmitRoundTrip pins the basic durable contract: Admit
// creates a receipt, a duplicate Admit returns it, and Get reads it back
// with the complete inner event intact.
func TestIngressReceiptAdmitRoundTrip(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	r := sampleReceipt("T1", "C1", "1700000000.000100")

	created, existing, err := s.Admit(r)
	if err != nil {
		t.Fatalf("Admit: %v", err)
	}
	if !created || existing != nil {
		t.Fatalf("first Admit: created=%v existing=%v; want true,nil", created, existing)
	}
	if r.ID == "" || !strings.HasPrefix(r.ID, "in-") {
		t.Fatalf("receipt id = %q; want non-empty in-* id", r.ID)
	}
	if r.Status != ingressStatusReceived {
		t.Fatalf("status = %q; want %q", r.Status, ingressStatusReceived)
	}
	if r.Generation != 1 {
		t.Fatalf("generation = %d; want 1", r.Generation)
	}

	got, err := s.Get(r.Origin)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got == nil {
		t.Fatal("Get returned nil for admitted receipt")
	}
	if got.APIAppID != "A0SWITCH" || got.EventID != r.EventID {
		t.Fatalf("Get lost outer identifiers: %+v", got)
	}
	var ev map[string]any
	if err := json.Unmarshal(got.Event, &ev); err != nil {
		t.Fatalf("inner event not valid JSON: %v", err)
	}
	if ev["text"] != "hi" || ev["ts"] != "1700000000.000100" {
		t.Fatalf("inner event not preserved: %v", ev)
	}
}

// TestIngressReceiptGetMissing confirms Get returns (nil, nil) for an
// origin with no receipt.
func TestIngressReceiptGetMissing(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	got, err := s.Get(ReceiptOrigin{TeamID: "T1", ChannelID: "C1", TS: "9.9"})
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got != nil {
		t.Fatalf("Get = %+v; want nil", got)
	}
}

// TestIngressReceiptDuplicateReturnsExisting is acceptance-rule-9 shaped:
// a second Admit of the same origin (a Slack redelivery) creates nothing
// and hands back the already-stored receipt.
func TestIngressReceiptDuplicateReturnsExisting(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	first := sampleReceipt("T1", "C1", "1700000000.000200")
	if _, _, err := s.Admit(first); err != nil {
		t.Fatalf("first Admit: %v", err)
	}

	dup := sampleReceipt("T1", "C1", "1700000000.000200")
	dup.EventID = "EvRedelivered"
	created, existing, err := s.Admit(dup)
	if err != nil {
		t.Fatalf("duplicate Admit: %v", err)
	}
	if created {
		t.Fatal("duplicate Admit created=true; want false")
	}
	if existing == nil {
		t.Fatal("duplicate Admit existing=nil; want the stored receipt")
	}
	// The stored receipt (first writer) wins; the redelivery body is ignored.
	if existing.EventID != first.EventID {
		t.Fatalf("existing.EventID = %q; want first writer %q", existing.EventID, first.EventID)
	}
}

// TestIngressReceiptConcurrentAdmitFirstWriterWins runs many concurrent
// Admits of the same origin and asserts exactly one create. Meaningful
// under `go test -race`.
func TestIngressReceiptConcurrentAdmitFirstWriterWins(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	origin := ReceiptOrigin{TeamID: "T1", ChannelID: "C1", TS: "1700000000.000300"}

	const n = 24
	var wg sync.WaitGroup
	var createdCount atomic.Int64
	var existingCount atomic.Int64
	errs := make(chan error, n)
	start := make(chan struct{})

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			r := &IngressReceipt{
				Origin:     origin,
				EventID:    fmt.Sprintf("Ev%d", i),
				ReceivedAt: time.Now().UTC(),
				Event:      json.RawMessage(fmt.Sprintf(`{"type":"message","n":%d}`, i)),
			}
			<-start
			created, existing, err := s.Admit(r)
			if err != nil {
				errs <- err
				return
			}
			if created {
				createdCount.Add(1)
			}
			if existing != nil {
				existingCount.Add(1)
			}
		}(i)
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent Admit error: %v", err)
	}
	if got := createdCount.Load(); got != 1 {
		t.Fatalf("created count = %d; want exactly 1", got)
	}
	if got := existingCount.Load(); got != n-1 {
		t.Fatalf("existing count = %d; want %d", got, n-1)
	}
}

// TestIngressReceiptCrashBetweenTempAndLink simulates a crash after the
// temp write but before the link (a stray ingress-*.tmp orphan). Admit
// must still cleanly create the receipt on the next attempt.
func TestIngressReceiptCrashBetweenTempAndLink(t *testing.T) {
	s, dir := newTestReceiptStore(t)

	// Simulate the orphan a crash-before-link would leave behind.
	orphan := filepath.Join(dir, "ingress-orphan.tmp")
	if err := os.WriteFile(orphan, []byte(`{"partial":true}`), 0o600); err != nil {
		t.Fatalf("seed orphan temp: %v", err)
	}

	r := sampleReceipt("T1", "C1", "1700000000.000400")
	created, existing, err := s.Admit(r)
	if err != nil {
		t.Fatalf("Admit after orphan temp: %v", err)
	}
	if !created || existing != nil {
		t.Fatalf("Admit after orphan: created=%v existing=%v; want true,nil", created, existing)
	}
	got, err := s.Get(r.Origin)
	if err != nil || got == nil {
		t.Fatalf("Get after orphan admit: got=%v err=%v", got, err)
	}
	// The orphan is ignored by scans (not a *.json file).
	pend, err := s.Pending()
	if err != nil {
		t.Fatalf("Pending: %v", err)
	}
	if len(pend) != 1 {
		t.Fatalf("Pending len = %d; want 1 (orphan temp must be ignored)", len(pend))
	}
}

// TestIngressReceiptUnfsyncedRenameCrash simulates a crash mid-rename
// during Update (temp present, final intact). A stray temp must not
// corrupt reads of the intact receipt.
func TestIngressReceiptUnfsyncedRenameCrash(t *testing.T) {
	s, dir := newTestReceiptStore(t)
	r := sampleReceipt("T1", "C1", "1700000000.000500")
	if _, _, err := s.Admit(r); err != nil {
		t.Fatalf("Admit: %v", err)
	}

	// A crash between an Update's temp write and its rename leaves the
	// final file intact and a stray temp behind.
	stray := filepath.Join(dir, "ingress-halfwritten.tmp")
	if err := os.WriteFile(stray, []byte("{ truncated"), 0o600); err != nil {
		t.Fatalf("seed stray temp: %v", err)
	}

	got, err := s.Get(r.Origin)
	if err != nil {
		t.Fatalf("Get with stray temp present: %v", err)
	}
	if got == nil || got.EventID != r.EventID {
		t.Fatalf("intact receipt not readable through stray temp: %+v", got)
	}
	pend, err := s.Pending()
	if err != nil {
		t.Fatalf("Pending: %v", err)
	}
	if len(pend) != 1 {
		t.Fatalf("Pending len = %d; want 1 (stray temp ignored)", len(pend))
	}
}

// TestIngressReceiptCorruptExistingQuarantinedThenClaimed writes an
// unparseable file at the final origin-keyed name, then Admit must
// quarantine it (*.corrupt) and successfully claim the origin on retry.
func TestIngressReceiptCorruptExistingQuarantinedThenClaimed(t *testing.T) {
	s, dir := newTestReceiptStore(t)
	origin := ReceiptOrigin{TeamID: "T1", ChannelID: "C1", TS: "1700000000.000600"}
	finalPath := s.pathForID(receiptID(origin))
	if err := os.WriteFile(finalPath, []byte("}{ not json"), 0o600); err != nil {
		t.Fatalf("seed corrupt receipt: %v", err)
	}

	r := sampleReceipt("T1", "C1", "1700000000.000600")
	created, existing, err := s.Admit(r)
	if err != nil {
		t.Fatalf("Admit over corrupt receipt: %v", err)
	}
	if !created || existing != nil {
		t.Fatalf("Admit over corrupt: created=%v existing=%v; want true,nil", created, existing)
	}
	if n := countCorruptFiles(t, dir); n != 1 {
		t.Fatalf("quarantined corrupt files = %d; want 1", n)
	}
	got, err := s.Get(origin)
	if err != nil || got == nil {
		t.Fatalf("Get after quarantine+claim: got=%v err=%v", got, err)
	}
	if got.EventID != r.EventID {
		t.Fatalf("claimed receipt mismatch: %+v", got)
	}
}

// TestIngressReceiptUpdateRoundTrip pins the happy-path generation-checked
// rewrite: Update advances the generation and persists the mutation.
func TestIngressReceiptUpdateRoundTrip(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	r := sampleReceipt("T1", "C1", "1700000000.000700")
	if _, _, err := s.Admit(r); err != nil {
		t.Fatalf("Admit: %v", err)
	}
	if r.Generation != 1 {
		t.Fatalf("post-Admit generation = %d; want 1", r.Generation)
	}

	r.Status = ingressStatusRouting
	r.Targets = map[string]TargetDelivery{
		"ollie-main": {Session: "ollie-main", Kind: "ambient", Status: "pending", IdempotencyKey: "ingress:" + r.ID + ":target:ollie-main"},
	}
	if err := s.Update(r); err != nil {
		t.Fatalf("Update: %v", err)
	}
	if r.Generation != 2 {
		t.Fatalf("post-Update generation = %d; want 2", r.Generation)
	}

	got, err := s.Get(r.Origin)
	if err != nil || got == nil {
		t.Fatalf("Get after Update: got=%v err=%v", got, err)
	}
	if got.Status != ingressStatusRouting || got.Generation != 2 {
		t.Fatalf("persisted receipt = status %q gen %d; want routing gen 2", got.Status, got.Generation)
	}
	if _, ok := got.Targets["ollie-main"]; !ok {
		t.Fatalf("target not persisted: %+v", got.Targets)
	}
}

// TestIngressReceiptUpdateStale is the lost-race contract: a second Update
// carrying the pre-bump generation is rejected with ErrStale rather than
// silently overwriting the winner.
func TestIngressReceiptUpdateStale(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	r := sampleReceipt("T1", "C1", "1700000000.000800")
	if _, _, err := s.Admit(r); err != nil {
		t.Fatalf("Admit: %v", err)
	}

	// Two readers observe generation 1.
	readerA, err := s.Get(r.Origin)
	if err != nil {
		t.Fatalf("Get A: %v", err)
	}
	readerB, err := s.Get(r.Origin)
	if err != nil {
		t.Fatalf("Get B: %v", err)
	}

	// A wins and bumps to generation 2.
	readerA.Status = ingressStatusDelivered
	if err := s.Update(readerA); err != nil {
		t.Fatalf("Update A: %v", err)
	}

	// B's stale write is rejected.
	readerB.Status = ingressStatusFailed
	err = s.Update(readerB)
	if !errors.Is(err, ErrStale) {
		t.Fatalf("Update B err = %v; want ErrStale", err)
	}

	// The winner's state stands.
	got, err := s.Get(r.Origin)
	if err != nil || got == nil {
		t.Fatalf("Get after conflict: got=%v err=%v", got, err)
	}
	if got.Status != ingressStatusDelivered {
		t.Fatalf("status = %q; want winner %q", got.Status, ingressStatusDelivered)
	}

	// B re-reads (generation 2) and its merged write now succeeds.
	readerB2, err := s.Get(r.Origin)
	if err != nil {
		t.Fatalf("Get B2: %v", err)
	}
	readerB2.Reason = "post-merge"
	if err := s.Update(readerB2); err != nil {
		t.Fatalf("Update B2 after re-read: %v", err)
	}
}

// TestIngressReceiptUpdateMissing rejects an Update for an origin that was
// never admitted.
func TestIngressReceiptUpdateMissing(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	r := sampleReceipt("T1", "C1", "1700000000.000900")
	r.Generation = 1
	if err := s.Update(r); err == nil {
		t.Fatal("Update of missing receipt returned nil; want error")
	}
}

// TestIngressReceiptPendingOrdering pins non-terminal ordering by
// (ReceivedAt, Origin.TS) and confirms terminal receipts are excluded.
func TestIngressReceiptPendingOrdering(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	base := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)

	mk := func(ts string, at time.Time, terminal bool) {
		r := sampleReceipt("T1", "C1", ts)
		r.ReceivedAt = at
		if _, _, err := s.Admit(r); err != nil {
			t.Fatalf("Admit %s: %v", ts, err)
		}
		if terminal {
			r.Status = ingressStatusDelivered
			if err := s.Update(r); err != nil {
				t.Fatalf("Update terminal %s: %v", ts, err)
			}
		}
	}

	// Two share a ReceivedAt (tie broken by TS); one is later; one is
	// terminal and must be excluded.
	mk("1700000000.000002", base, false)                  // same time, higher ts -> second
	mk("1700000000.000001", base, false)                  // same time, lower ts  -> first
	mk("1700000000.000003", base.Add(time.Second), false) // later time -> third
	mk("1700000000.000004", base.Add(-time.Second), true) // earliest but terminal -> excluded

	pend, err := s.Pending()
	if err != nil {
		t.Fatalf("Pending: %v", err)
	}
	gotTS := make([]string, len(pend))
	for i, r := range pend {
		gotTS[i] = r.Origin.TS
	}
	wantTS := []string{"1700000000.000001", "1700000000.000002", "1700000000.000003"}
	if len(gotTS) != len(wantTS) {
		t.Fatalf("Pending TS = %v; want %v", gotTS, wantTS)
	}
	for i := range wantTS {
		if gotTS[i] != wantTS[i] {
			t.Fatalf("Pending order = %v; want %v", gotTS, wantTS)
		}
	}
	// Guard the ordering invariant directly.
	if !sort.SliceIsSorted(pend, func(i, j int) bool {
		if !pend[i].ReceivedAt.Equal(pend[j].ReceivedAt) {
			return pend[i].ReceivedAt.Before(pend[j].ReceivedAt)
		}
		return pend[i].Origin.TS < pend[j].Origin.TS
	}) {
		t.Fatalf("Pending not sorted by (ReceivedAt, TS): %v", gotTS)
	}
}

// TestIngressReceiptPendingQuarantinesCorrupt confirms a corrupt file in
// the directory is quarantined during a Pending scan and never fails it.
func TestIngressReceiptPendingQuarantinesCorrupt(t *testing.T) {
	s, dir := newTestReceiptStore(t)
	good := sampleReceipt("T1", "C1", "1700000000.001000")
	if _, _, err := s.Admit(good); err != nil {
		t.Fatalf("Admit good: %v", err)
	}
	// A corrupt receipt file that is not a duplicate of any admitted origin.
	if err := os.WriteFile(filepath.Join(dir, "in-bogus.json"), []byte("nonsense"), 0o600); err != nil {
		t.Fatalf("seed corrupt: %v", err)
	}

	pend, err := s.Pending()
	if err != nil {
		t.Fatalf("Pending: %v", err)
	}
	if len(pend) != 1 || pend[0].Origin.TS != good.Origin.TS {
		t.Fatalf("Pending = %+v; want only the good receipt", pend)
	}
	if n := countCorruptFiles(t, dir); n != 1 {
		t.Fatalf("quarantined files = %d; want 1", n)
	}
}

// TestIngressReceiptSweepRetentionFloor rejects a retention below 24h.
func TestIngressReceiptSweepRetentionFloor(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	for _, d := range []time.Duration{0, time.Hour, 23*time.Hour + 59*time.Minute} {
		if _, err := s.Sweep(d); err == nil {
			t.Fatalf("Sweep(%s) err = nil; want retention-floor rejection", d)
		}
	}
	// Exactly the floor and above are accepted.
	if _, err := s.Sweep(24 * time.Hour); err != nil {
		t.Fatalf("Sweep(24h) err = %v; want nil", err)
	}
}

// TestIngressReceiptSweepRemovesOnlyOldTerminal proves Sweep removes
// terminal receipts older than retention, keeps recent terminal receipts,
// and never touches non-terminal receipts regardless of age.
func TestIngressReceiptSweepRemovesOnlyOldTerminal(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	now := time.Now().UTC()

	admit := func(ts string, at time.Time, status string) *IngressReceipt {
		r := sampleReceipt("T1", "C1", ts)
		r.ReceivedAt = at
		if _, _, err := s.Admit(r); err != nil {
			t.Fatalf("Admit %s: %v", ts, err)
		}
		if status != ingressStatusReceived {
			r.Status = status
			if err := s.Update(r); err != nil {
				t.Fatalf("Update %s: %v", ts, err)
			}
		}
		return r
	}

	oldTerminal := admit("1700000000.100001", now.Add(-8*24*time.Hour), ingressStatusDelivered)
	recentTerminal := admit("1700000000.100002", now.Add(-1*time.Hour), ingressStatusFailed)
	oldNonTerminal := admit("1700000000.100003", now.Add(-30*24*time.Hour), ingressStatusReceived)

	removed, err := s.Sweep(7 * 24 * time.Hour)
	if err != nil {
		t.Fatalf("Sweep: %v", err)
	}
	if removed != 1 {
		t.Fatalf("removed = %d; want 1 (only the old terminal receipt)", removed)
	}

	if got, _ := s.Get(oldTerminal.Origin); got != nil {
		t.Fatal("old terminal receipt survived sweep")
	}
	if got, _ := s.Get(recentTerminal.Origin); got == nil {
		t.Fatal("recent terminal receipt wrongly swept")
	}
	if got, _ := s.Get(oldNonTerminal.Origin); got == nil {
		t.Fatal("old NON-terminal receipt wrongly swept")
	}
}

// TestIngressReceiptSweepQuarantinesCorrupt confirms a corrupt scan entry
// during Sweep is quarantined, not fatal.
func TestIngressReceiptSweepQuarantinesCorrupt(t *testing.T) {
	s, dir := newTestReceiptStore(t)
	if err := os.WriteFile(filepath.Join(dir, "in-garbage.json"), []byte("<<not json>>"), 0o600); err != nil {
		t.Fatalf("seed corrupt: %v", err)
	}
	removed, err := s.Sweep(7 * 24 * time.Hour)
	if err != nil {
		t.Fatalf("Sweep: %v", err)
	}
	if removed != 0 {
		t.Fatalf("removed = %d; want 0", removed)
	}
	if n := countCorruptFiles(t, dir); n != 1 {
		t.Fatalf("quarantined files = %d; want 1", n)
	}
}

// TestIngressReceiptWriteFailures confirms the counter starts at zero and
// increments when a persistence attempt fails. The store directory is
// replaced with a regular file so CreateTemp fails for any uid (ENOTDIR),
// keeping the test valid under root as well as an unprivileged user.
func TestIngressReceiptWriteFailures(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "chat-ingress")
	s, err := NewIngressReceiptStore(dir)
	if err != nil {
		t.Fatalf("NewIngressReceiptStore: %v", err)
	}
	if got := s.WriteFailures(); got != 0 {
		t.Fatalf("initial WriteFailures = %d; want 0", got)
	}

	// Turn the store directory into a regular file: any temp create now
	// fails with ENOTDIR.
	if err := os.RemoveAll(dir); err != nil {
		t.Fatalf("RemoveAll: %v", err)
	}
	if err := os.WriteFile(dir, []byte("x"), 0o600); err != nil {
		t.Fatalf("replace dir with file: %v", err)
	}

	r := sampleReceipt("T1", "C1", "1700000000.110000")
	if _, _, err := s.Admit(r); err == nil {
		t.Fatal("Admit into non-directory store returned nil error; want failure")
	}
	if got := s.WriteFailures(); got != 1 {
		t.Fatalf("WriteFailures after failed Admit = %d; want 1", got)
	}
}

// TestSafeStorageIDHashesHostileComponents pins the sanitizer: safe short
// tokens pass through; hostile or overlong components are hashed, keeping
// derived filenames inside the store directory.
func TestSafeStorageIDHashesHostileComponents(t *testing.T) {
	if got := safeStorageID("T012ABC", "team"); got != "T012ABC" {
		t.Fatalf("safe token altered: %q", got)
	}
	if got := safeStorageID("1700000000.000100", "ts"); got != "1700000000.000100" {
		t.Fatalf("ts token altered: %q", got)
	}
	hostile := safeStorageID("../../etc/passwd", "chan")
	if !strings.HasPrefix(hostile, "chan-") || strings.ContainsAny(hostile, "/.") {
		t.Fatalf("hostile component not sanitized: %q", hostile)
	}
	long := safeStorageID(strings.Repeat("a", maxSafeComponentLen+1), "ts")
	if !strings.HasPrefix(long, "ts-") {
		t.Fatalf("overlong component not hashed: %q", long)
	}

	// A hostile ts must not let the receipt filename escape the store dir.
	id := receiptID(ReceiptOrigin{TeamID: "T1", ChannelID: "C1", TS: "../../evil"})
	if strings.ContainsAny(id, "/") {
		t.Fatalf("receipt id contains a path separator: %q", id)
	}
}

// TestReceiptIDDeterministicAndDistinct confirms the origin key is stable
// for a given origin and distinct across origins (including the
// component-delimiter collision that a bare concatenation would confuse).
func TestReceiptIDDeterministicAndDistinct(t *testing.T) {
	a := ReceiptOrigin{TeamID: "T1", ChannelID: "C1", TS: "1.1"}
	if receiptID(a) != receiptID(a) {
		t.Fatal("receiptID not deterministic")
	}
	// ("a-b","c") vs ("a","b-c") would collide under naive "a-b-c" joining.
	x := receiptID(ReceiptOrigin{TeamID: "a-b", ChannelID: "c", TS: "t"})
	y := receiptID(ReceiptOrigin{TeamID: "a", ChannelID: "b-c", TS: "t"})
	if x == y {
		t.Fatalf("distinct origins collided: %q == %q", x, y)
	}
}

// TestIngressReceiptFilePermissions confirms receipts are 0600 in a 0700
// directory.
func TestIngressReceiptFilePermissions(t *testing.T) {
	s, dir := newTestReceiptStore(t)
	r := sampleReceipt("T1", "C1", "1700000000.120000")
	if _, _, err := s.Admit(r); err != nil {
		t.Fatalf("Admit: %v", err)
	}
	di, err := os.Stat(dir)
	if err != nil {
		t.Fatalf("stat dir: %v", err)
	}
	if perm := di.Mode().Perm(); perm != 0o700 {
		t.Fatalf("dir perm = %o; want 700", perm)
	}
	fi, err := os.Stat(s.pathForID(r.ID))
	if err != nil {
		t.Fatalf("stat receipt: %v", err)
	}
	if perm := fi.Mode().Perm(); perm != 0o600 {
		t.Fatalf("receipt perm = %o; want 600", perm)
	}
}

// TestIngressReceiptUpdatedAtSetOnAdmitAndUpdate — F8: UpdatedAt is written
// on Admit and refreshed on every Update, so the sweep's stale-reclaim
// window can read it as the claim timestamp.
func TestIngressReceiptUpdatedAtSetOnAdmitAndUpdate(t *testing.T) {
	s, _ := newTestReceiptStore(t)
	r := sampleReceipt("T1", "C1", "1700000000.130000")
	// A clearly-past ReceivedAt so the later real-clock Update is strictly
	// after the Admit timestamp regardless of when the test runs.
	r.ReceivedAt = time.Now().Add(-time.Hour).UTC()
	if _, _, err := s.Admit(r); err != nil {
		t.Fatalf("Admit: %v", err)
	}
	got, err := s.Get(r.Origin)
	if err != nil || got == nil {
		t.Fatalf("Get after Admit: got=%v err=%v", got, err)
	}
	if got.UpdatedAt.IsZero() {
		t.Fatal("UpdatedAt zero after Admit")
	}
	admitTS := got.UpdatedAt

	got.Status = ingressStatusRouting
	if err := s.Update(got); err != nil {
		t.Fatalf("Update: %v", err)
	}
	after, err := s.Get(r.Origin)
	if err != nil || after == nil {
		t.Fatalf("Get after Update: got=%v err=%v", after, err)
	}
	if !after.UpdatedAt.After(admitTS) {
		t.Errorf("UpdatedAt not advanced by Update: admit=%v update=%v", admitTS, after.UpdatedAt)
	}
}

// TestNewIngressReceiptStoreRejectsSymlinkedDir — F7: a symlinked store dir
// is refused at construction (parity with openRegistryFile's symlink guard),
// so a redirected chat-ingress cannot capture receipt writes.
func TestNewIngressReceiptStoreRejectsSymlinkedDir(t *testing.T) {
	root := t.TempDir()
	realDir := filepath.Join(root, "real-ingress")
	if err := os.MkdirAll(realDir, 0o700); err != nil {
		t.Fatalf("mkdir real: %v", err)
	}
	link := filepath.Join(root, "chat-ingress")
	if err := os.Symlink(realDir, link); err != nil {
		t.Fatalf("symlink: %v", err)
	}
	_, err := NewIngressReceiptStore(link)
	if err == nil {
		t.Fatal("NewIngressReceiptStore(symlinked dir): want error, got nil")
	}
	if !strings.Contains(err.Error(), "symlink") {
		t.Errorf("error = %v, want mention of 'symlink'", err)
	}
}

// TestIngressReceiptReadRejectsSymlinkedFile — F7: a receipt file swapped
// for a symlink is not followed (O_NOFOLLOW). Get errors rather than reading
// the link target, and the scan quarantines it instead of returning it.
func TestIngressReceiptReadRejectsSymlinkedFile(t *testing.T) {
	s, dir := newTestReceiptStore(t)
	r := sampleReceipt("T1", "C1", "1700000000.140000")
	if _, _, err := s.Admit(r); err != nil {
		t.Fatalf("Admit: %v", err)
	}
	// Replace the on-disk receipt with a symlink to an outside file.
	victim := filepath.Join(t.TempDir(), "secret.json")
	if err := os.WriteFile(victim, []byte(`{"id":"evil","status":"delivered"}`), 0o600); err != nil {
		t.Fatalf("seed victim: %v", err)
	}
	path := s.pathForID(r.ID)
	if err := os.Remove(path); err != nil {
		t.Fatalf("remove receipt: %v", err)
	}
	if err := os.Symlink(victim, path); err != nil {
		t.Fatalf("symlink receipt: %v", err)
	}

	if _, err := s.Get(r.Origin); err == nil {
		t.Fatal("Get followed a symlinked receipt file; want error (O_NOFOLLOW)")
	}
	pending, err := s.Pending()
	if err != nil {
		t.Fatalf("Pending: %v", err)
	}
	if len(pending) != 0 {
		t.Errorf("Pending returned %d receipts through a symlink, want 0", len(pending))
	}
	if countCorruptFiles(t, dir) == 0 {
		t.Error("symlinked receipt was not quarantined")
	}
	// The victim file outside the store was never touched.
	if b, _ := os.ReadFile(victim); string(b) != `{"id":"evil","status":"delivered"}` {
		t.Errorf("victim file mutated through the store: %q", b)
	}
}
