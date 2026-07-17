package main

import (
	"encoding/json"
	"regexp"
)

// AuthorClass is the fail-closed classification of a company-room message's
// author. Only human authors can wake sessions in Phase 1; every bot class
// is retained so the reason a message delivered nothing is machine
// readable and so the Phase 2 company-bot mention leg can turn on without
// changing the classifier.
type AuthorClass int

const (
	AuthorHuman      AuthorClass = iota // has a user id, no bot id, allowlisted subtype
	AuthorCompanyBot                    // bot resolving to a registered agent (single-agent resolution lands in Phase 2)
	AuthorBot                           // any other bot / webhook / integration author
	AuthorSelf                          // the switchboard's own bot user
)

func (c AuthorClass) String() string {
	switch c {
	case AuthorHuman:
		return "human"
	case AuthorCompanyBot:
		return "company_bot"
	case AuthorBot:
		return "bot"
	case AuthorSelf:
		return "self"
	default:
		return "unknown"
	}
}

// Wake kinds recorded on each WakeTarget and mirrored into the receipt's
// per-target delivery record.
const (
	wakeKindAmbient  = "ambient"
	wakeKindTargeted = "targeted"
)

// Machine-readable no-delivery reasons. An empty Reason on a RouteDecision
// with a non-empty Wakes set means "delivered normally".
const (
	wakeReasonNoDirectory          = "no_directory"           // no directory loaded; caller falls through to legacy
	wakeReasonNotCompanyRoom       = "not_company_room"       // channel is not an imported company room
	wakeReasonSubtypeNotAdmissible = "subtype_not_admissible" // defense-in-depth subtype gate
	wakeReasonCompanySelf          = "company_self"           // switchboard's own post
	wakeReasonCompanyBotPhase2     = "company_bot_phase2"     // registered company bot; mention leg lands in Phase 2
	wakeReasonUnknownBot           = "unknown_bot"            // unknown bot / webhook / integration
	wakeReasonNoAmbientMembers     = "no_ambient_members"     // unmentioned human, room has no ambient wake set
	wakeReasonMentionedNoEligible  = "mentioned_no_eligible"  // mentioned agents are not member+eligible
)

// AdmissibleSubtype reports whether a Slack message subtype is admissible:
// "" (absent), "file_share", "thread_broadcast", or "bot_message".
// Everything else (channel_join, channel_topic, hidden edit/delete records,
// etc.) is not admitted.
func AdmissibleSubtype(subtype string) bool {
	switch subtype {
	case "", "file_share", "thread_broadcast", "bot_message":
		return true
	default:
		return false
	}
}

// CompanyMessage is the minimal decoded inner Slack event the router needs.
// Blocks is the raw `blocks` array so the mention extractor can walk
// rich_text without the router committing to Slack's full block schema.
type CompanyMessage struct {
	TeamID, ChannelID, TS, ThreadTS string
	UserID, BotID                   string
	Subtype                         string
	Text                            string
	Blocks                          json.RawMessage
}

// WakeTarget names one agent to wake and why (ambient vs targeted).
type WakeTarget struct {
	Agent CompanyAgent
	Kind  string // wakeKindAmbient | wakeKindTargeted
}

// RouteDecision is the pure routing result. Room == nil means the channel
// is not a company room and the caller must fall through to the legacy
// path. A non-nil Room with an empty Wakes set means the company path
// admitted the message but woke nobody; Reason says why.
type RouteDecision struct {
	Room   *CompanyRoom
	Author AuthorClass
	Wakes  []WakeTarget
	Reason string
}

// companyMentionTokenRE matches a canonical Slack user mention token in
// top-level text: <@U012ABC> or <@U012ABC|display label>. Subteam
// (<!subteam^…>), channel (<#C…>), and special (<!here>) tokens are
// intentionally not matched — only user mentions can name a company agent.
var companyMentionTokenRE = regexp.MustCompile(`<@([A-Z0-9]+)(?:\|[^>]*)?>`)

// richTextElement is one node in a Slack rich_text block. Mentions appear
// as {"type":"user","user_id":"U…"} leaves; container nodes
// (rich_text_section, rich_text_list, …) carry nested Elements.
type richTextElement struct {
	Type     string            `json:"type"`
	UserID   string            `json:"user_id"`
	Elements []richTextElement `json:"elements"`
}

// richTextBlock is a top-level message block; only "rich_text" blocks carry
// user mention leaves.
type richTextBlock struct {
	Type     string            `json:"type"`
	Elements []richTextElement `json:"elements"`
}

// ExtractMentionIDs returns the union of user IDs found as rich_text "user"
// elements in blocks and as canonical <@U…> / <@U…|label> tokens in text,
// deduplicated with block-order-then-text-order preserved. Neither source
// alone is sufficient: Slack guarantees rich_text blocks only for end-user
// client messages, while bot-composed messages may carry mentions only as
// text tokens. Parsing is defensive — malformed blocks contribute nothing
// rather than erroring, and plain text that merely resembles "@name"
// carries no canonical token and matches nobody.
func ExtractMentionIDs(blocks json.RawMessage, text string) []string {
	seen := make(map[string]bool)
	var out []string
	add := func(id string) {
		if id == "" || seen[id] {
			return
		}
		seen[id] = true
		out = append(out, id)
	}
	for _, id := range mentionIDsFromBlocks(blocks) {
		add(id)
	}
	for _, m := range companyMentionTokenRE.FindAllStringSubmatch(text, -1) {
		add(m[1])
	}
	return out
}

func mentionIDsFromBlocks(blocks json.RawMessage) []string {
	if len(blocks) == 0 {
		return nil
	}
	var parsed []richTextBlock
	if err := json.Unmarshal(blocks, &parsed); err != nil {
		return nil // defensive: unparseable blocks contribute no mentions
	}
	var out []string
	var walk func(els []richTextElement)
	walk = func(els []richTextElement) {
		for i := range els {
			e := els[i]
			if e.Type == "user" && e.UserID != "" {
				out = append(out, e.UserID)
			}
			if len(e.Elements) > 0 {
				walk(e.Elements)
			}
		}
	}
	for i := range parsed {
		if parsed[i].Type == "rich_text" {
			walk(parsed[i].Elements)
		}
	}
	return out
}

// ComputeWakeSet is the pure routing core (no I/O). It implements the
// design doc's routing table fail-closed:
//
//   - non-company channel                -> Room == nil (fall through to legacy)
//   - non-allowlisted subtype            -> no wakes (defense in depth)
//   - switchboard self / any bot author  -> no wakes in Phase 1, reason recorded
//   - human, no native company mention   -> the room's ambient_wake members
//   - human, one+ native company mention -> only the mentioned member+eligible
//     agents (ambient suppressed; mentions are exclusive)
//
// Native mentions are matched exactly against directory bot_user_ids;
// a matched id still passes membership and mention-eligibility before it
// wakes anyone, and each named receiver wakes at most once.
func ComputeWakeSet(dir *CompanyDirectory, msg CompanyMessage, selfBotUserID string) RouteDecision {
	if dir == nil {
		return RouteDecision{Reason: wakeReasonNoDirectory}
	}
	room, ok := dir.RoomByChannel(msg.TeamID, msg.ChannelID)
	if !ok {
		return RouteDecision{Reason: wakeReasonNotCompanyRoom}
	}
	dec := RouteDecision{Room: room}

	// Defense in depth: the HTTP admission gate already dropped
	// non-allowlisted subtypes, but re-check so this function can never
	// wake anyone on one.
	if !AdmissibleSubtype(msg.Subtype) {
		dec.Reason = wakeReasonSubtypeNotAdmissible
		return dec
	}

	dec.Author = classifyAuthor(dir, msg, selfBotUserID)
	switch dec.Author {
	case AuthorSelf:
		dec.Reason = wakeReasonCompanySelf
		return dec
	case AuthorCompanyBot:
		// Phase 1 delivers nothing for bot authors; the mention-driven
		// company-bot wake leg turns on in Phase 2.
		dec.Reason = wakeReasonCompanyBotPhase2
		return dec
	case AuthorBot:
		dec.Reason = wakeReasonUnknownBot
		return dec
	}

	// AuthorHuman.
	var companyMentions []*CompanyAgent
	for _, id := range ExtractMentionIDs(msg.Blocks, msg.Text) {
		if a, ok := dir.AgentByBotUserID(id); ok {
			companyMentions = append(companyMentions, a)
		}
	}

	if len(companyMentions) > 0 {
		// Exclusive mention routing: ambient is suppressed entirely, even
		// when no mentioned agent turns out member+eligible.
		seen := make(map[string]bool)
		for _, a := range companyMentions {
			if !dir.IsMember(room, a.Name) || !dir.IsMentionEligible(room, a.Name) {
				continue
			}
			if seen[a.BotUserID] {
				continue
			}
			seen[a.BotUserID] = true
			dec.Wakes = append(dec.Wakes, WakeTarget{Agent: *a, Kind: wakeKindTargeted})
		}
		if len(dec.Wakes) == 0 {
			dec.Reason = wakeReasonMentionedNoEligible
		}
		return dec
	}

	// Ambient routing: an unmentioned human message wakes the room's
	// ambient set (validated ⊆ members at parse time).
	seen := make(map[string]bool)
	for _, name := range room.AmbientWake {
		a, ok := dir.AgentByName(name)
		if !ok {
			continue
		}
		if seen[a.BotUserID] {
			continue
		}
		seen[a.BotUserID] = true
		dec.Wakes = append(dec.Wakes, WakeTarget{Agent: *a, Kind: wakeKindAmbient})
	}
	if len(dec.Wakes) == 0 {
		dec.Reason = wakeReasonNoAmbientMembers
	}
	return dec
}

// classifyAuthor applies the fail-closed author classification. Self is
// checked first (the switchboard's own bot user id); then any bot author
// (bot_message subtype or a non-empty bot id) is either a registered
// company bot — when its user field resolves to a directory agent — or an
// unknown bot. A message with neither a user nor a bot identity is
// malformed and treated as a bot (no delivery).
func classifyAuthor(dir *CompanyDirectory, msg CompanyMessage, selfBotUserID string) AuthorClass {
	if selfBotUserID != "" && msg.UserID == selfBotUserID {
		return AuthorSelf
	}
	if msg.Subtype == "bot_message" || msg.BotID != "" {
		if msg.UserID != "" {
			if _, ok := dir.AgentByBotUserID(msg.UserID); ok {
				return AuthorCompanyBot
			}
		}
		return AuthorBot
	}
	if msg.UserID == "" {
		return AuthorBot
	}
	return AuthorHuman
}
