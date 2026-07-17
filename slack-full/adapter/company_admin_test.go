package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// company_admin_test.go — Slack company-rooms Phase 3b operator-surface
// coverage: the two-leg redrive (frozen-target reset preserving the recorded
// IdempotencyKey byte-for-byte; attempts_exhausted requiring --include-failed;
// target-less reset-to-received re-entering correlation), 409 on a held
// single-flight, 404 past retention, and the filtered receipts listing.

func doRedrive(t *testing.T, h *companyHarness, body map[string]any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/internal/company/redrive", bytes.NewReader(raw))
	w := httptest.NewRecorder()
	h.gw.handleCompanyRedrive(w, req)
	var out map[string]any
	if w.Body.Len() > 0 {
		_ = json.Unmarshal(w.Body.Bytes(), &out)
	}
	return w.Code, out
}

func doReceipts(t *testing.T, h *companyHarness, query string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/internal/company/receipts"+query, nil)
	w := httptest.NewRecorder()
	h.gw.handleCompanyReceipts(w, req)
	var out map[string]any
	if w.Body.Len() > 0 {
		_ = json.Unmarshal(w.Body.Bytes(), &out)
	}
	return w.Code, out
}

func resetTargetSet(out map[string]any) map[string]bool {
	set := map[string]bool{}
	if list, ok := out["reset_targets"].([]any); ok {
		for _, v := range list {
			if s, ok := v.(string); ok {
				set[s] = true
			}
		}
	}
	return set
}

func admitTargetsReceipt(t *testing.T, h *companyHarness, ts string, targets map[string]TargetDelivery) *IngressReceipt {
	t.Helper()
	ev := slackMessageEvent{Type: "message", User: "Uhuman", Channel: testChannelID, TS: ts, Text: "x"}
	raw, _ := json.Marshal(ev)
	origin := ReceiptOrigin{TeamID: testTeamID, ChannelID: testChannelID, TS: ts}
	r := &IngressReceipt{Origin: origin, Event: raw, Status: ingressStatusRouting, Targets: targets}
	if created, _, err := h.receipts.Admit(r); err != nil || !created {
		t.Fatalf("admit targets receipt: created=%v err=%v", created, err)
	}
	return r
}

// TestRedriveResetsSelectedTargetsPreservesIdempotencyKey — leg 1: a failed
// bound target is reset to pending and re-delivered under the SAME recorded
// IdempotencyKey (never re-derived); a delivered sibling target is untouched.
func TestRedriveResetsSelectedTargetsPreservesIdempotencyKey(t *testing.T) {
	gc := newFakeGC(t)
	df := baseDirectoryFile()
	bf := baseBindingsFile()
	h := newCompanyHarness(t, gc.server.URL, &df, &bf, 4)
	setFixedClock(h)

	const frozenKey = "ingress:frozen:custom-key"
	r := admitTargetsReceipt(t, h, "1700000000.001000", map[string]TargetDelivery{
		companyBoundTargetKeyPrefix + "ollie-main": {
			Session: "ollie-main", Kind: "ambient", Status: companyTargetFailed,
			Detail: "gc 500", Attempts: 3, IdempotencyKey: frozenKey, Agent: "ollie",
		},
		companyBoundTargetKeyPrefix + "riley-main": {
			Session: "riley-main", Kind: "ambient", Status: companyTargetDelivered,
			IdempotencyKey: "ingress:done:riley", Agent: "riley",
		},
	})

	code, out := doRedrive(t, h, map[string]any{"receipt": r.ID, "targets": []string{}, "include_failed": false})
	if code != http.StatusOK {
		t.Fatalf("redrive HTTP %d, body=%+v", code, out)
	}
	if out["leg"] != "targets" {
		t.Errorf("leg = %v, want targets", out["leg"])
	}
	if set := resetTargetSet(out); !set["ollie-main"] || set["riley-main"] {
		t.Errorf("reset_targets = %v, want only ollie-main", out["reset_targets"])
	}
	h.wait()

	calls := gc.sessionCalls()
	if len(calls) != 1 {
		t.Fatalf("delivery calls = %d, want 1 (only the reset target)", len(calls))
	}
	if calls[0].idemKey != frozenKey {
		t.Errorf("Idempotency-Key = %q, want preserved %q", calls[0].idemKey, frozenKey)
	}
	final, _ := h.gw.store().Get(r.Origin)
	if final.Status != ingressStatusDelivered {
		t.Errorf("final status = %s, want delivered", final.Status)
	}
	if td := final.Targets[companyBoundTargetKeyPrefix+"ollie-main"]; td.IdempotencyKey != frozenKey {
		t.Errorf("reset target key mutated to %q, want %q", td.IdempotencyKey, frozenKey)
	}
}

func TestRedriveAttemptsExhaustedRequiresIncludeFailed(t *testing.T) {
	gc := newFakeGC(t)
	df := baseDirectoryFile()
	bf := baseBindingsFile()
	h := newCompanyHarness(t, gc.server.URL, &df, &bf, 4)
	setFixedClock(h)

	r := admitTargetsReceipt(t, h, "1700000000.001010", map[string]TargetDelivery{
		companyBoundTargetKeyPrefix + "ollie-main": {
			Session: "ollie-main", Kind: "ambient", Status: companyTargetFailed,
			Detail:         companyReasonAttemptsExhausted + " after 12 attempts: gc 500",
			Attempts:       12,
			IdempotencyKey: "ingress:exhausted:ollie", Agent: "ollie",
		},
	})

	// Without --include-failed the exhausted target is left alone.
	code, out := doRedrive(t, h, map[string]any{"receipt": r.ID, "include_failed": false})
	if code != http.StatusOK {
		t.Fatalf("redrive HTTP %d", code)
	}
	if set := resetTargetSet(out); len(set) != 0 {
		t.Errorf("reset_targets = %v, want empty without include_failed", out["reset_targets"])
	}
	h.wait()
	if n := len(gc.sessionCalls()); n != 0 {
		t.Fatalf("delivery = %d, want 0 (exhausted not touched)", n)
	}
	if got, _ := h.gw.store().Get(r.Origin); got.Targets[companyBoundTargetKeyPrefix+"ollie-main"].Status != companyTargetFailed {
		t.Error("exhausted target changed without include_failed")
	}

	// With --include-failed it is reset and re-delivered.
	code, out = doRedrive(t, h, map[string]any{"receipt": r.ID, "include_failed": true})
	if code != http.StatusOK {
		t.Fatalf("redrive(include_failed) HTTP %d", code)
	}
	if set := resetTargetSet(out); !set["ollie-main"] {
		t.Errorf("reset_targets = %v, want ollie-main with include_failed", out["reset_targets"])
	}
	h.wait()
	if n := len(gc.sessionCalls()); n != 1 {
		t.Errorf("delivery = %d, want 1 after include_failed", n)
	}
}

// TestRedriveReresolvesUnboundTargetAfterBindingRepair — leg 1, F2: a failed-
// unbound target (frozen with Session=="" when its binding was stale at route
// time) is re-resolved from its recorded Agent name against the CURRENT bindings
// (now repaired), bound to the resolved session with a freshly-derived
// idempotency key, reset to pending, and delivered.
func TestRedriveReresolvesUnboundTargetAfterBindingRepair(t *testing.T) {
	gc := newFakeGC(t)
	df := baseDirectoryFile()
	bf := baseBindingsFile() // current bindings resolve ollie -> ollie-main (repaired)
	h := newCompanyHarness(t, gc.server.URL, &df, &bf, 4)
	setFixedClock(h)

	r := admitTargetsReceipt(t, h, "1700000000.001030", map[string]TargetDelivery{
		companyUnboundTargetKeyPrefix + "ollie": {
			Kind:   "ambient",
			Status: companyTargetFailed,
			Detail: "no company binding for (room=orchestrator-team, agent=ollie)",
			Agent:  "ollie",
			// No Session and no IdempotencyKey — this is the unbound freeze shape.
		},
	})

	code, out := doRedrive(t, h, map[string]any{"receipt": r.ID})
	if code != http.StatusOK {
		t.Fatalf("redrive HTTP %d, body=%+v", code, out)
	}
	if set := resetTargetSet(out); !set["ollie-main"] {
		t.Errorf("reset_targets = %v, want ollie-main (re-resolved)", out["reset_targets"])
	}
	h.wait()

	wantKey := companyIdempotencyKey(r.ID, "ollie-main")
	calls := gc.sessionCalls()
	if len(calls) != 1 {
		t.Fatalf("delivery calls = %d, want 1 (re-resolved target delivered)", len(calls))
	}
	if calls[0].idemKey != wantKey {
		t.Errorf("Idempotency-Key = %q, want derived %q", calls[0].idemKey, wantKey)
	}
	final, _ := h.gw.store().Get(r.Origin)
	if final.Status != ingressStatusDelivered {
		t.Errorf("final status = %s, want delivered", final.Status)
	}
	td, ok := final.Targets[companyBoundTargetKeyPrefix+"ollie-main"]
	if !ok || td.Session != "ollie-main" || td.IdempotencyKey != wantKey {
		t.Errorf("re-resolved target = %+v (ok=%v), want bound ollie-main with derived key", td, ok)
	}
	if _, stillUnbound := final.Targets[companyUnboundTargetKeyPrefix+"ollie"]; stillUnbound {
		t.Error("unbound target key was not relocated after re-resolution")
	}
}

// TestRedriveStillUnboundReturns422 — leg 1, F2: an unbound failed target whose
// agent still does not resolve to a session is NOT a success-shaped empty reset;
// the endpoint returns 422 with a machine-readable reason and the operator has an
// explicit signal to repair the binding.
func TestRedriveStillUnboundReturns422(t *testing.T) {
	gc := newFakeGC(t)
	df := baseDirectoryFile()
	bf := baseBindingsFile() // "ghost" has no binding
	h := newCompanyHarness(t, gc.server.URL, &df, &bf, 4)
	setFixedClock(h)

	r := admitTargetsReceipt(t, h, "1700000000.001031", map[string]TargetDelivery{
		companyUnboundTargetKeyPrefix + "ghost": {
			Kind:   "ambient",
			Status: companyTargetFailed,
			Detail: "no company binding for (room=orchestrator-team, agent=ghost)",
			Agent:  "ghost",
		},
	})

	code, out := doRedrive(t, h, map[string]any{"receipt": r.ID})
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("still-unbound redrive HTTP %d, want 422 (body=%+v)", code, out)
	}
	if out["reason"] != "unresolved_targets" {
		t.Errorf("reason = %v, want unresolved_targets", out["reason"])
	}
	unres := map[string]bool{}
	if list, ok := out["unresolvable"].([]any); ok {
		for _, v := range list {
			if s, ok := v.(string); ok {
				unres[s] = true
			}
		}
	}
	if !unres["ghost"] {
		t.Errorf("unresolvable = %v, want ghost", out["unresolvable"])
	}
	h.wait()
	if n := len(gc.sessionCalls()); n != 0 {
		t.Errorf("delivery = %d, want 0 (nothing deliverable)", n)
	}
	// The receipt must be untouched (no generation churn, still failed).
	if got, _ := h.gw.store().Get(r.Origin); got.Status != ingressStatusRouting {
		// admitted as routing; a 422 must not flip it. (No commit occurred.)
		t.Errorf("status = %s, want routing (unchanged)", got.Status)
	}
}

// TestRedriveRejectsMalformedReceiptID — F4: a traversal- or NUL-shaped receipt
// id is rejected with 400 before any filesystem path is derived from it.
func TestRedriveRejectsMalformedReceiptID(t *testing.T) {
	gc := newFakeGC(t)
	df := baseDirectoryFile()
	bf := baseBindingsFile()
	h := newCompanyHarness(t, gc.server.URL, &df, &bf, 4)

	for _, bad := range []string{
		"../../../../etc/hostname",
		"in-../../etc/passwd",
		"in-ok/../../escape",
		"not-a-receipt",
		"in-",
		"in-bad\x00null",
	} {
		code, out := doRedrive(t, h, map[string]any{"receipt": bad})
		if code != http.StatusBadRequest {
			t.Errorf("redrive receipt=%q HTTP %d, want 400 (body=%+v)", bad, code, out)
		}
	}
}

// TestRedriveTargetlessResetToReceived — leg 2: a target-less
// correlation_recovery_exhausted receipt is reset to received and re-enters
// correlation; with the record now materialized it claims and delivers.
func TestRedriveTargetlessResetToReceived(t *testing.T) {
	gc := newFakeGC(t)
	df := baseDirectoryFile()
	bf := baseBindingsFile()
	h := newCompanyHarness(t, gc.server.URL, &df, &bf, 4)
	setFixedClock(h)
	h.gw.authors = fakeAuthorResolver{byBot: map[string]companyBotInfo{
		"B0RILEY": {UserID: botRiley, AppID: "A0AAAAAA2"},
	}}
	writeHarnessRecord(t, h, pendingRecord(delegationTS))

	origin := ReceiptOrigin{TeamID: testTeamID, ChannelID: testChannelID, TS: resultTS}
	ev := botEvent("B0RILEY", botRiley, "A0AAAAAA2", resultTS, humanRootTS,
		"<@"+botOllie+"> done", resultMetadata(fixtureNonce, delegationTS))
	raw, _ := json.Marshal(ev)
	if created, _, err := h.receipts.Admit(&IngressReceipt{
		Origin: origin, Event: raw, Status: ingressStatusFailed,
		Reason: companyReasonRecoveryExhausted, RecoveryReason: peerParkCorrelationPending,
		RecoveryAttempts: companyPeerRecoveryMaxAttempts,
	}); err != nil || !created {
		t.Fatalf("admit exhausted receipt: %v", err)
	}

	code, out := doRedrive(t, h, map[string]any{
		"origin": map[string]string{"team_id": testTeamID, "channel_id": testChannelID, "ts": resultTS},
	})
	if code != http.StatusOK {
		t.Fatalf("redrive HTTP %d, body=%+v", code, out)
	}
	if out["leg"] != "correlation" {
		t.Errorf("leg = %v, want correlation", out["leg"])
	}
	h.wait()

	if n := len(gc.sessionCalls()); n != 1 {
		t.Fatalf("delivery = %d, want 1 (re-entered correlation and delivered)", n)
	}
	final, _ := h.gw.store().Get(origin)
	if final.Status != ingressStatusDelivered {
		t.Errorf("final status = %s, want delivered", final.Status)
	}
	if final.RecoveryAttempts != 0 || final.RecoveryReason != "" || final.Reason != "" {
		t.Errorf("recovery not cleared: attempts=%d reason=%q park=%q",
			final.RecoveryAttempts, final.RecoveryReason, final.Reason)
	}
}

func TestRedrive404PastRetention(t *testing.T) {
	gc := newFakeGC(t)
	df := baseDirectoryFile()
	bf := baseBindingsFile()
	h := newCompanyHarness(t, gc.server.URL, &df, &bf, 4)

	code, out := doRedrive(t, h, map[string]any{
		"origin": map[string]string{"team_id": testTeamID, "channel_id": testChannelID, "ts": "1700000000.009999"},
	})
	if code != http.StatusNotFound {
		t.Fatalf("redrive of absent receipt HTTP %d, want 404 (body=%+v)", code, out)
	}
}

func TestRedrive409SingleFlightHeld(t *testing.T) {
	gc := newFakeGC(t)
	df := baseDirectoryFile()
	bf := baseBindingsFile()
	h := newCompanyHarness(t, gc.server.URL, &df, &bf, 4)

	r := admitTargetsReceipt(t, h, "1700000000.001020", map[string]TargetDelivery{
		companyBoundTargetKeyPrefix + "ollie-main": {
			Session: "ollie-main", Kind: "ambient", Status: companyTargetFailed, IdempotencyKey: "k", Agent: "ollie",
		},
	})
	// Hold the single-flight as a concurrent delivery would.
	if !h.gw.acquireSingleFlight(r.ID) {
		t.Fatal("could not seed single-flight")
	}
	defer h.gw.releaseSingleFlight(r.ID)

	code, _ := doRedrive(t, h, map[string]any{"receipt": r.ID})
	if code != http.StatusConflict {
		t.Fatalf("redrive with held single-flight HTTP %d, want 409", code)
	}
}

// TestCompanyReceiptsListingFilters — the receipts endpoint returns the
// operator view and honors the origin / root / status filters.
func TestCompanyReceiptsListingFilters(t *testing.T) {
	gc := newFakeGC(t)
	df := baseDirectoryFile()
	bf := baseBindingsFile()
	h := newCompanyHarness(t, gc.server.URL, &df, &bf, 4)

	rootA := "1700000000.000001"
	rootB := "1700000000.000002"
	mk := func(ts, threadTS, status string, mutate func(*IngressReceipt)) *IngressReceipt {
		ev := slackMessageEvent{Type: "message", User: "Uhuman", Channel: testChannelID, TS: ts, ThreadTS: threadTS, Text: "x"}
		raw, _ := json.Marshal(ev)
		r := &IngressReceipt{Origin: ReceiptOrigin{testTeamID, testChannelID, ts}, Event: raw, Status: status}
		if mutate != nil {
			mutate(r)
		}
		if created, _, err := h.receipts.Admit(r); err != nil || !created {
			t.Fatalf("admit %s: %v", ts, err)
		}
		return r
	}
	r1 := mk("1700000000.001100", rootA, ingressStatusRouting, nil)
	_ = mk("1700000000.001101", rootA, ingressStatusReceived, func(r *IngressReceipt) {
		r.Reason = peerParkCorrelationPending
		r.RecoveryReason = peerParkCorrelationPending
		r.RecoveryAttempts = 2
		r.RecoveryNextAt = time.Now().UTC().Add(time.Minute)
	})
	r3 := mk("1700000000.001102", rootB, ingressStatusDelivered, nil)

	// No filter: all three.
	code, out := doReceipts(t, h, "")
	if code != http.StatusOK {
		t.Fatalf("receipts HTTP %d", code)
	}
	if got := len(receiptList(out)); got != 3 {
		t.Fatalf("unfiltered receipts = %d, want 3", got)
	}

	// status filter.
	_, out = doReceipts(t, h, "?status=routing")
	if list := receiptList(out); len(list) != 1 || list[0]["id"] != r1.ID {
		t.Errorf("status=routing = %+v, want only r1", list)
	}

	// origin filter.
	_, out = doReceipts(t, h, "?origin="+testTeamID+":"+testChannelID+":"+r3.Origin.TS)
	if list := receiptList(out); len(list) != 1 || list[0]["id"] != r3.ID {
		t.Errorf("origin filter = %+v, want only r3", list)
	}

	// root filter (derived thread root).
	_, out = doReceipts(t, h, "?root="+testTeamID+":"+testChannelID+":"+rootA)
	if list := receiptList(out); len(list) != 2 {
		t.Errorf("root=A receipts = %d, want 2", len(list))
	}

	// Shape: parked receipt surfaces the recovery fields.
	_, out = doReceipts(t, h, "?status=received")
	list := receiptList(out)
	if len(list) != 1 {
		t.Fatalf("status=received = %d, want 1", len(list))
	}
	pv := list[0]
	if pv["reason"] != peerParkCorrelationPending {
		t.Errorf("view reason = %v, want correlation_pending", pv["reason"])
	}
	if got, _ := pv["recovery_attempts"].(float64); int(got) != 2 {
		t.Errorf("view recovery_attempts = %v, want 2", pv["recovery_attempts"])
	}
	if _, ok := pv["origin"].(map[string]any); !ok {
		t.Errorf("view missing origin object: %+v", pv)
	}
	if _, ok := pv["targets"].([]any); !ok {
		t.Errorf("view missing targets array: %+v", pv)
	}
}

func receiptList(out map[string]any) []map[string]any {
	var list []map[string]any
	if raw, ok := out["receipts"].([]any); ok {
		for _, v := range raw {
			if m, ok := v.(map[string]any); ok {
				list = append(list, m)
			}
		}
	}
	return list
}
