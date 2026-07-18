package main

import (
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
	"time"
)

// company_turn.go — the per-session current-turn pointer the Go delivery
// worker writes before every company wake (Phase 2c). It is the deterministic
// context source the Python verbs read (no receipt scanning). Field order
// matches company-current-turn/<session>.json in the golden fixtures.

const companyCurrentTurnSchemaV = 1

// companyCurrentTurn mirrors company-current-turn/<session>.json.
type companyCurrentTurn struct {
	SchemaVersion int    `json:"schema_version"`
	Session       string `json:"session"`
	ReceiptID     string `json:"receipt_id"`
	TeamID        string `json:"team_id"`
	ChannelID     string `json:"channel_id"`
	TS            string `json:"ts"`
	Room          string `json:"room"`
	Kind          string `json:"kind"`
	ThreadRootTS  string `json:"thread_root_ts"`
	Agent         string `json:"agent"`
	DelegationKey string `json:"delegation_key,omitempty"`
	// City is the target session's gc city when a city-qualified binding
	// delivered this turn cross-city; empty = the adapter's own city.
	City        string `json:"city,omitempty"`
	DeliveredAt string `json:"delivered_at"`
}

// writeCurrentTurnPointer atomically writes the pointer for one wake into
// turnsDir/<session>.json. Called before the gc session POST so the pointer
// is durable before the turn can act on it. An unset turnsDir is a
// misconfiguration (never true in production, where main resolves a default):
// it is rejected rather than silently writing to the process CWD.
//
// The session name is passed through the shared filename-component sanitizer
// before it becomes a filename: an operator-supplied name containing '/', a
// leading '.', ".." or any byte outside [A-Za-z0-9._-] is hashed to a safe
// component so a hostile bind can never escape turnsDir. A well-formed session
// name is filename-safe and passes through unchanged (byte-parity with the
// Python reader for the common case). The pointer's `session` JSON field keeps
// the raw name.
func writeCurrentTurnPointer(turnsDir string, p companyCurrentTurn) error {
	if strings.TrimSpace(turnsDir) == "" {
		return errors.New("company: current-turn dir unset")
	}
	data, err := marshalCurrentTurn(p)
	if err != nil {
		return err
	}
	filename := companySanitizeComponent(p.Session) + ".json"
	return companyWriteFileAtomic(filepath.Join(turnsDir, filename), data)
}

// marshalCurrentTurn encodes the pointer with the byte-for-byte fixture shape
// (2-space indent, fixture field order).
func marshalCurrentTurn(p companyCurrentTurn) ([]byte, error) {
	return json.MarshalIndent(p, "", "  ")
}

// companyPointerFromTarget builds a current-turn pointer from a receipt and one
// frozen target.
func companyPointerFromTarget(r *IngressReceipt, room *CompanyRoom, td TargetDelivery, threadRootTS string, now time.Time) companyCurrentTurn {
	roomName := ""
	if room != nil {
		roomName = room.Name
	}
	return companyCurrentTurn{
		SchemaVersion: companyCurrentTurnSchemaV,
		Session:       td.Session,
		City:          td.City,
		ReceiptID:     r.ID,
		TeamID:        r.Origin.TeamID,
		ChannelID:     r.Origin.ChannelID,
		TS:            r.Origin.TS,
		Room:          roomName,
		Kind:          td.Kind,
		ThreadRootTS:  threadRootTS,
		Agent:         td.Agent,
		DelegationKey: td.DelegationKey,
		DeliveredAt:   now.UTC().Format(time.RFC3339),
	}
}
