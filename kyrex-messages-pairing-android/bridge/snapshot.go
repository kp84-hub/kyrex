// SPDX-License-Identifier: AGPL-3.0-or-later
package pairbridge

import (
	"context"
	"encoding/json"
	"errors"
	"go.mau.fi/mautrix-gmessages/pkg/libgm/gmproto"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

type snapshotMessage struct {
	ID             string `json:"id"`
	ConversationID string `json:"conversation_id"`
	Conversation   string `json:"conversation"`
	Sender         string `json:"sender"`
	Number         string `json:"number"`
	Received       string `json:"received"`
	Body           string `json:"body"`
	Kind           string `json:"kind"` // Current conversation protocol, not delivery proof.
	Direction      string `json:"direction"`
	timestamp      int64
}

func limited(value string, n int) string {
	runes := []rune(value)
	if len(runes) > n {
		return string(runes[:n])
	}
	return value
}
func snapshotRow(c *gmproto.Conversation, m *gmproto.Message) (snapshotMessage, bool) {
	if m == nil || m.GetMessageID() == "" || (m.GetConversationID() != "" && m.GetConversationID() != c.GetConversationID()) {
		return snapshotMessage{}, false
	}
	var body strings.Builder
	for _, part := range m.GetMessageInfo() {
		body.WriteString(part.GetMessageContent().GetContent())
	}
	if body.Len() == 0 || utf8.RuneCountInString(body.String()) > 10000 {
		return snapshotMessage{}, false
	}
	row := snapshotMessage{ID: limited(m.GetMessageID(), 200), ConversationID: limited(c.GetConversationID(), 200), Conversation: limited(c.GetName(), 200), Body: body.String(), Kind: c.GetType().String(), Direction: "unknown", timestamp: m.GetTimestamp()}
	if row.Kind != "SMS" && row.Kind != "RCS" {
		row.Kind = "UNKNOWN"
	}
	if m.GetTimestamp() > 0 {
		row.Received = time.UnixMicro(m.GetTimestamp()).UTC().Format(time.RFC3339Nano)
	}
	participant := m.GetSenderParticipant()
	for _, p := range c.GetParticipants() {
		if p.GetID().GetParticipantID() == m.GetParticipantID() && m.GetParticipantID() != "" {
			participant = p
			break
		}
	}
	if participant != nil {
		row.Sender = limited(participant.GetFullName(), 200)
		row.Number = limited(participant.GetID().GetNumber(), 100)
		row.Direction = "incoming"
		if participant.GetIsMe() || (c.GetDefaultOutgoingID() != "" && participant.GetID().GetParticipantID() == c.GetDefaultOutgoingID()) {
			row.Direction = "outgoing"
			row.Sender = "You"
		}
	}
	return row, true
}

// Snapshot reads text only. RPC failure preserves the cloud's last complete
// snapshot. No Google credentials or media are included in the result.
func (b *Bridge) Snapshot() (string, error) {
	ctx, cancel := context.WithTimeout(b.ctx, 90*time.Second)
	defer cancel()
	resp, err := boundedCall(ctx, 20*time.Second, func() (*gmproto.ListConversationsResponse, error) {
		return b.client.ListConversations(10, gmproto.ListConversationsRequest_INBOX)
	})
	if err != nil {
		return "", errors.New("Phone snapshot could not load. Open Google Messages and reconnect; previous cloud snapshot kept.")
	}
	conversations := resp.GetConversations()
	if len(conversations) > 10 {
		conversations = conversations[:10]
	}
	rows := []snapshotMessage{}
	seen := map[string]bool{}
	for _, c := range conversations {
		page, err := boundedCall(ctx, 20*time.Second, func() (*gmproto.ListMessagesResponse, error) {
			return b.client.FetchMessages(c.GetConversationID(), 10, nil)
		})
		if err != nil {
			return "", errors.New("Phone snapshot incomplete. Reconnect and sync again; previous cloud snapshot kept.")
		}
		messages := page.GetMessages()
		if len(messages) > 10 {
			messages = messages[:10]
		}
		for _, m := range messages {
			row, ok := snapshotRow(c, m)
			key := row.ConversationID + "\x00" + row.ID
			if ok && !seen[key] {
				rows = append(rows, row)
				seen[key] = true
			}
		}
	}
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].timestamp > rows[j].timestamp })
	if ctx.Err() != nil {
		return "", errors.New("Phone sync timed out; previous cloud snapshot kept.")
	}
	data, err := json.Marshal(map[string]any{"messages": rows})
	return string(data), err
}
