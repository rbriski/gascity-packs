package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"
)

// company_admin.go — the operator redrive surface (Slack company-rooms Phase
// 3b), served on the adapter's internal mux only. Two receipt-native endpoints
// back the `gc slack company-status` / `company-redrive` Python verbs, which are
// thin clients (the receipt store keeps exactly one writer — Go, generation
// CAS — so the verbs never rewrite receipts):
//
//   - GET  /internal/company/receipts?origin=…|root=…|status=…  bounded listing
//   - POST /internal/company/redrive                            two-leg redrive
//
// Both operate under the receipt's in-process single-flight and the store's
// generation-checked commit, exactly like the delivery worker.

// adminTargetView is the per-target slice of a receipt exposed to the operator
// verbs (session, kind, status, attempts, detail, delegation_key).
type adminTargetView struct {
	Session       string `json:"session,omitempty"`
	Agent         string `json:"agent,omitempty"`
	Kind          string `json:"kind"`
	Status        string `json:"status"`
	Attempts      int    `json:"attempts"`
	Detail        string `json:"detail,omitempty"`
	DelegationKey string `json:"delegation_key,omitempty"`
}

// adminReceiptView is one receipt's operator-facing shape: id/origin/status/
// reason, the S7 recovery fields, the ack cursor, and the per-target state.
type adminReceiptView struct {
	ID               string            `json:"id"`
	Origin           ReceiptOrigin     `json:"origin"`
	Status           string            `json:"status"`
	Reason           string            `json:"reason,omitempty"`
	RecoveryAttempts int               `json:"recovery_attempts,omitempty"`
	RecoveryNextAt   string            `json:"recovery_next_at,omitempty"`
	RecoveryReason   string            `json:"recovery_reason,omitempty"`
	AckState         string            `json:"ack_state,omitempty"`
	ReceivedAt       string            `json:"received_at,omitempty"`
	UpdatedAt        string            `json:"updated_at,omitempty"`
	Targets          []adminTargetView `json:"targets"`
}

func newAdminReceiptView(r *IngressReceipt) adminReceiptView {
	v := adminReceiptView{
		ID:               r.ID,
		Origin:           r.Origin,
		Status:           r.Status,
		Reason:           r.Reason,
		RecoveryAttempts: r.RecoveryAttempts,
		RecoveryReason:   r.RecoveryReason,
		AckState:         r.AckState,
		Targets:          []adminTargetView{},
	}
	if !r.RecoveryNextAt.IsZero() {
		v.RecoveryNextAt = r.RecoveryNextAt.UTC().Format(time.RFC3339)
	}
	if !r.ReceivedAt.IsZero() {
		v.ReceivedAt = r.ReceivedAt.UTC().Format(time.RFC3339)
	}
	if !r.UpdatedAt.IsZero() {
		v.UpdatedAt = r.UpdatedAt.UTC().Format(time.RFC3339)
	}
	for _, td := range r.Targets {
		v.Targets = append(v.Targets, adminTargetView{
			Session:       td.Session,
			Agent:         td.Agent,
			Kind:          td.Kind,
			Status:        td.Status,
			Attempts:      td.Attempts,
			Detail:        td.Detail,
			DelegationKey: td.DelegationKey,
		})
	}
	return v
}

// handleCompanyReceipts serves GET /internal/company/receipts: a bounded JSON
// listing of receipts, filtered by origin (team:channel:ts), root
// (team:channel:root_ts), and/or status. Read-only.
func (g *companyGateway) handleCompanyReceipts(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	store := g.storeOrNil()
	if store == nil {
		writeAdminError(w, http.StatusServiceUnavailable, "receipt store unavailable")
		return
	}
	originFilter, ok := parseTripleParam(w, req.URL.Query().Get("origin"), "origin")
	if !ok {
		return
	}
	rootFilter, ok := parseTripleParam(w, req.URL.Query().Get("root"), "root")
	if !ok {
		return
	}
	statusFilter := req.URL.Query().Get("status")

	all, err := store.List()
	if err != nil {
		writeAdminError(w, http.StatusInternalServerError, "list receipts: "+err.Error())
		return
	}
	views := make([]adminReceiptView, 0, len(all))
	for _, r := range all {
		if originFilter != nil && r.Origin != *originFilter {
			continue
		}
		if statusFilter != "" && r.Status != statusFilter {
			continue
		}
		if rootFilter != nil {
			root, ok := rootOfMsg(r.Origin, decodeCompanyMessage(r.Origin, r.Event))
			if !ok || root.TeamID != rootFilter.TeamID ||
				root.ChannelID != rootFilter.ChannelID ||
				root.ThreadRootTS != rootFilter.TS {
				continue
			}
		}
		views = append(views, newAdminReceiptView(r))
	}
	writeAdminJSON(w, http.StatusOK, map[string]any{"receipts": views})
}

// companyRedriveRequest is the POST /internal/company/redrive body: a receipt id
// OR an origin triple, the optional target-session filter, and the
// include_failed flag (required to touch an attempts_exhausted target).
type companyRedriveRequest struct {
	Receipt       string         `json:"receipt"`
	Origin        *ReceiptOrigin `json:"origin"`
	Targets       []string       `json:"targets"`
	IncludeFailed bool           `json:"include_failed"`
}

// handleCompanyRedrive serves POST /internal/company/redrive. It resolves the
// receipt, holds its single-flight (409 when held elsewhere), and applies one of
// the two redrive legs under a generation-checked commit, then re-triggers
// delivery. A terminal receipt swept past retention is 404.
func (g *companyGateway) handleCompanyRedrive(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	store := g.storeOrNil()
	if store == nil {
		writeAdminError(w, http.StatusServiceUnavailable, "receipt store unavailable")
		return
	}
	var body companyRedriveRequest
	if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
		writeAdminError(w, http.StatusBadRequest, "decode: "+err.Error())
		return
	}
	id, selector, err := redriveReceiptID(body)
	if err != nil {
		writeAdminError(w, http.StatusBadRequest, err.Error())
		return
	}

	// Single-flight: a concurrent delivery or redrive holding the receipt's
	// claim gets a 409 (the verb retries). Released before triggerDelivery so
	// the spawned delivery goroutine can re-acquire it.
	if !g.acquireSingleFlight(id) {
		writeAdminError(w, http.StatusConflict, "receipt single-flight held elsewhere")
		return
	}

	r, gerr := store.GetByID(id)
	if gerr != nil {
		g.releaseSingleFlight(id)
		writeAdminError(w, http.StatusInternalServerError, "read receipt: "+gerr.Error())
		return
	}
	if r == nil {
		g.releaseSingleFlight(id)
		writeAdminError(w, http.StatusNotFound, "receipt "+selector+" not found (terminal and swept, or never admitted)")
		return
	}

	res, cerr := g.applyRedrive(r, body)
	g.releaseSingleFlight(id)
	if cerr != nil {
		writeAdminError(w, http.StatusInternalServerError, "redrive commit: "+cerr.Error())
		return
	}
	if res.empty {
		// Empty effective selection with recoverable-but-unbound targets: an
		// explicit machine-readable 422, never a success-shaped empty reset that
		// a client would mistake for a completed redrive (the parked result wake
		// is still lost). The operator must repair the binding, then re-run.
		writeAdminJSON(w, http.StatusUnprocessableEntity, map[string]any{
			"receipt":      r.ID,
			"leg":          res.leg,
			"reason":       "unresolved_targets",
			"error":        fmt.Sprintf("redrive selected no deliverable target: %d unbound target(s) do not resolve to a session under the current bindings", len(res.unresolvable)),
			"unresolvable": res.unresolvable,
			"status":       r.Status,
		})
		return
	}
	if res.trigger {
		g.triggerDelivery(r.Origin)
	}
	writeAdminJSON(w, http.StatusOK, map[string]any{
		"receipt":       r.ID,
		"leg":           res.leg,
		"reset_targets": res.resetSessions,
		"unresolvable":  res.unresolvable,
		"status":        r.Status,
	})
}

// redriveOutcome is the result of applyRedrive: the sessions reset to pending,
// the agent names of failed-unbound targets that still do not resolve to a
// session (surfaced to the operator), the leg taken, whether a delivery should
// be triggered, and whether the leg-1 selection was empty-but-unresolvable (a
// 422, never a 200 empty reset).
type redriveOutcome struct {
	resetSessions []string
	unresolvable  []string
	leg           string
	trigger       bool
	empty         bool
}

// applyRedrive mutates the receipt in place under a generation-checked commit
// per the two legs (S7 / redrive parity). The caller holds the receipt's
// single-flight and releases it before triggering.
func (g *companyGateway) applyRedrive(r *IngressReceipt, body companyRedriveRequest) (redriveOutcome, error) {
	// Leg 2: no recorded targets (a correlation_recovery_exhausted / parked
	// receipt) — reset to received, clear reason + recovery fields, re-enter
	// correlation from first-routing resolution.
	if len(r.Targets) == 0 {
		if cerr := g.commitReceipt(r, func(cur *IngressReceipt) {
			cur.Status = ingressStatusReceived
			cur.Reason = ""
			cur.RecoveryAttempts = 0
			cur.RecoveryNextAt = time.Time{}
			cur.RecoveryReason = ""
		}); cerr != nil {
			return redriveOutcome{leg: "correlation"}, cerr
		}
		return redriveOutcome{resetSessions: []string{}, unresolvable: []string{}, leg: "correlation", trigger: true}, nil
	}

	// Leg 1: frozen targets — reset the selected failed targets to pending.
	//
	// A bound failed target keeps its recorded IdempotencyKey byte-for-byte. A
	// failed-UNBOUND target (frozen with Session=="" when its binding was stale
	// at route time) is re-resolved here from its recorded Agent name against the
	// CURRENT bindings snapshot: on success it is bound to the resolved session,
	// given a freshly-derived idempotency key, and reset to pending; on failure
	// it stays unbound and is surfaced as unresolvable. An empty reset that still
	// has unresolvable unbound targets is a 422 (never a silent success).
	leg := "targets"
	targetFilter := map[string]bool{}
	for _, s := range body.Targets {
		targetFilter[s] = true
	}
	room, _ := g.dirStore.Snapshot().RoomByChannel(r.Origin.TeamID, r.Origin.ChannelID)
	bindings := g.bindStore.Snapshot()

	resetSessions := []string{}
	unresolvable := []string{}
	// plan maps a current target key to the session it will be reset under; a
	// key present here is committed to pending.
	plan := map[string]string{}
	for key, td := range r.Targets {
		if td.Status != companyTargetFailed {
			continue
		}
		if td.Session != "" {
			if !redriveSelectsTarget(td, targetFilter, body.IncludeFailed) {
				continue
			}
			plan[key] = td.Session
			resetSessions = append(resetSessions, td.Session)
			continue
		}
		// Failed-unbound target: re-resolve from the recorded Agent name.
		if td.Agent == "" {
			continue // no agent recorded — cannot re-resolve (defensive)
		}
		session := ""
		if room != nil {
			if s, ok := bindings.SessionFor(room.Name, td.Agent); ok {
				session = s
			}
		}
		// Scope: default (no --target) selects every failed-unbound target; a
		// --target filter matches the recorded agent name or the resolved session.
		if len(targetFilter) > 0 && !targetFilter[td.Agent] && (session == "" || !targetFilter[session]) {
			continue
		}
		if session == "" {
			unresolvable = append(unresolvable, td.Agent)
			continue
		}
		plan[key] = session
		resetSessions = append(resetSessions, session)
	}

	if len(resetSessions) == 0 {
		if len(unresolvable) > 0 {
			// Recoverable-but-unbound targets remain: 422, not a success-shaped
			// empty reset.
			return redriveOutcome{resetSessions: resetSessions, unresolvable: unresolvable, leg: leg, empty: true}, nil
		}
		// Genuinely nothing to do (all delivered, filtered out, or an
		// attempts_exhausted target without --include-failed): benign no-op, no
		// generation churn, no re-trigger.
		return redriveOutcome{resetSessions: resetSessions, unresolvable: unresolvable, leg: leg}, nil
	}

	if cerr := g.commitReceipt(r, func(cur *IngressReceipt) {
		now := g.now().UTC()
		for key, session := range plan {
			td, ok := cur.Targets[key]
			if !ok {
				continue
			}
			if td.Session == "" {
				// Newly re-resolved unbound target: bind the session, derive the
				// idempotency key per the standard formula, and relocate it from the
				// unbound key namespace to the bound one.
				td.Session = session
				td.IdempotencyKey = companyIdempotencyKey(cur.ID, session)
				delete(cur.Targets, key)
				key = companyBoundTargetKeyPrefix + session
			}
			// A bound target's IdempotencyKey is left untouched (never re-derived).
			td.Status = companyTargetPending
			td.Attempts = 0
			td.Detail = "operator_redrive"
			td.UpdatedAt = now
			cur.Targets[key] = td
		}
		cur.Status = ingressStatusRouting
		cur.Reason = ""
		cur.RecoveryAttempts = 0
		cur.RecoveryNextAt = time.Time{}
		cur.RecoveryReason = ""
	}); cerr != nil {
		return redriveOutcome{leg: leg}, cerr
	}
	return redriveOutcome{resetSessions: resetSessions, unresolvable: unresolvable, leg: leg, trigger: true}, nil
}

// redriveSelectsTarget reports whether a redrive touches this BOUND target: only
// a failed, session-bearing target is eligible here; --target restricts to the
// listed sessions; an attempts_exhausted target requires include_failed. Failed-
// unbound targets (Session=="") are handled separately in applyRedrive, which
// re-resolves them against the current bindings.
func redriveSelectsTarget(td TargetDelivery, targetFilter map[string]bool, includeFailed bool) bool {
	if td.Status != companyTargetFailed || td.Session == "" {
		return false
	}
	if len(targetFilter) > 0 && !targetFilter[td.Session] {
		return false
	}
	if strings.HasPrefix(td.Detail, companyReasonAttemptsExhausted) && !includeFailed {
		return false
	}
	return true
}

// redriveReceiptID resolves the receipt id and a human-readable selector from a
// redrive request: an explicit receipt id, or the derived id of an origin
// triple. Exactly one must be present.
func redriveReceiptID(body companyRedriveRequest) (id, selector string, err error) {
	if body.Receipt != "" {
		if body.Origin != nil {
			return "", "", errBadRequest("provide exactly one of receipt or origin")
		}
		// Validate the id shape before any path use (^in-[A-Za-z0-9._-]+$): a
		// traversal- or NUL-shaped receipt id must be rejected here, not joined
		// into a store path by GetByID.
		if !isReceiptID(body.Receipt) {
			return "", "", errBadRequest("receipt must match the receipt-id shape in-<...>")
		}
		return body.Receipt, body.Receipt, nil
	}
	if body.Origin == nil {
		return "", "", errBadRequest("one of receipt or origin is required")
	}
	o := *body.Origin
	if o.TeamID == "" || o.ChannelID == "" || o.TS == "" {
		return "", "", errBadRequest("origin requires team_id, channel_id, and ts")
	}
	return receiptID(o), o.TeamID + ":" + o.ChannelID + ":" + o.TS, nil
}

type adminBadRequest struct{ msg string }

func (e adminBadRequest) Error() string { return e.msg }
func errBadRequest(msg string) error    { return adminBadRequest{msg: msg} }

// storeOrNil returns the gateway's receipt store or nil (degraded).
func (g *companyGateway) storeOrNil() *IngressReceiptStore {
	if g == nil {
		return nil
	}
	return g.store()
}

// parseTripleParam parses a "a:b:c" query param into a ReceiptOrigin-shaped
// triple (team, channel, ts-or-root_ts). Empty is a nil filter (no restriction);
// a malformed value writes a 400 and reports ok=false.
func parseTripleParam(w http.ResponseWriter, value, what string) (*ReceiptOrigin, bool) {
	if value == "" {
		return nil, true
	}
	parts := strings.Split(value, ":")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" || parts[2] == "" {
		writeAdminError(w, http.StatusBadRequest, what+" must be team:channel:ts")
		return nil, false
	}
	return &ReceiptOrigin{TeamID: parts[0], ChannelID: parts[1], TS: parts[2]}, true
}

func writeAdminJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		log.Printf("company admin: encode response: %v", err)
	}
}

func writeAdminError(w http.ResponseWriter, status int, msg string) {
	writeAdminJSON(w, status, map[string]any{"error": msg})
}
