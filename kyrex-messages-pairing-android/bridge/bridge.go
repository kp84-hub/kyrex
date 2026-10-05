// SPDX-License-Identifier: AGPL-3.0-or-later
// A minimal gomobile adapter for local history and explicitly confirmed sends.
package pairbridge

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"github.com/rs/zerolog"
	"go.mau.fi/mautrix-gmessages/pkg/libgm"
	"go.mau.fi/mautrix-gmessages/pkg/libgm/events"
	"go.mau.fi/mautrix-gmessages/pkg/libgm/gmproto"
	"google.golang.org/protobuf/encoding/protojson"
)

type Sink interface {
	OnEvent(kind string, value string)
}
type Bridge struct {
	client  *libgm.Client
	sink    Sink
	ctx     context.Context
	cancel  context.CancelFunc
	closed  atomic.Bool
	draftMu sync.Mutex
	draft   *sendDraft
}

func NewBridge(saved string, sink Sink) (*Bridge, error) {
	auth := libgm.NewAuthData()
	if saved != "" {
		if err := json.Unmarshal([]byte(saved), auth); err != nil {
			return nil, errors.New("saved pairing could not be read")
		}
		if auth.Browser == nil || auth.RequestCrypto == nil || auth.RefreshKey == nil {
			return nil, errors.New("saved pairing is incomplete")
		}
	}
	b := &Bridge{sink: sink}
	b.ctx, b.cancel = context.WithCancel(context.Background())
	// Library logs can include message contents: disable them completely.
	logger := zerolog.Nop()
	b.ctx = logger.WithContext(b.ctx)
	b.client = libgm.NewClient(auth, nil, logger)
	b.client.SetPingInterval(20 * time.Minute)
	b.client.SetEventHandler(b.handle)
	return b, nil
}
func (b *Bridge) emit(kind, value string) {
	if !b.closed.Load() && b.sink != nil {
		b.sink.OnEvent(kind, value)
	}
}
func (b *Bridge) handle(evt any) {
	switch e := evt.(type) {
	case *libgm.WrappedMessage:
		if !e.IsOld {
			b.emit("NEW_MESSAGE", "")
		}
	case *events.GaiaLoggedOut:
		b.emit("UNPAIRED", "")
	case *events.AuthTokenRefreshed:
		b.emit("SAVE", "")
	case *events.ListenFatalError:
		b.emit("ERROR", safeError(e.Error).Error())
	case *events.ListenTemporaryError, *events.PhoneNotResponding, *events.NoDataReceived:
		b.emit("OFFLINE", "Phone or connection unavailable. Open Google Messages, then reconnect.")
	case *events.ListenRecovered, *events.PhoneRespondingAgain:
		b.emit("RECOVERED", "")
	}
}
func safeError(err error) error {
	switch {
	case errors.Is(err, libgm.ErrNoDevicesFound):
		return errors.New("No phone found. Use the Google account selected in Google Messages > Device pairing.")
	case errors.Is(err, libgm.ErrIncorrectEmoji):
		return errors.New("The emoji did not match. Tap Connect Messages to try again.")
	case errors.Is(err, libgm.ErrPairingCancelled):
		return errors.New("Pairing was cancelled on the phone.")
	case errors.Is(err, libgm.ErrPairingTimeout), errors.Is(err, libgm.ErrPairingInitTimeout), errors.Is(err, context.DeadlineExceeded):
		return errors.New("Pairing timed out. Open Google Messages and try again.")
	case errors.Is(err, context.Canceled):
		return errors.New("Connection cancelled.")
	case errors.Is(err, events.ErrInvalidCredentials):
		return errors.New("Google rejected or expired the session. Connect Messages again.")
	}
	var he events.HTTPError
	if errors.As(err, &he) && he.Resp != nil {
		return fmt.Errorf("Google request failed (HTTP %d). Try reconnecting; no messages were sent.", he.Resp.StatusCode)
	}
	return errors.New("Could not complete the Google Messages connection. Open Google Messages, check internet access, and reconnect.")
}
func (b *Bridge) Pair(cookies string) error {
	var jar map[string]string
	if json.Unmarshal([]byte(cookies), &jar) != nil || jar["SAPISID"] == "" {
		return errors.New("Google sign-in did not provide a complete session.")
	}
	b.client.AuthData.SetCookies(jar)
	ctx, cancel := context.WithTimeout(b.ctx, 90*time.Second)
	defer cancel()
	emoji, session, err := b.client.StartGaiaPairing(ctx)
	if err != nil {
		b.client.Disconnect()
		return safeError(err)
	}
	b.emit("EMOJI", emoji)
	if _, err = b.client.FinishGaiaPairing(ctx, session); err != nil {
		b.client.Disconnect()
		return safeError(err)
	}
	// Use the split API so reconnect happens once, under this worker's control.
	if err = b.client.Reconnect(); err != nil {
		return safeError(err)
	}
	return nil
}
func (b *Bridge) Connect() error {
	if err := b.client.Connect(); err != nil {
		return safeError(err)
	}
	return nil
}
func (b *Bridge) waitReady() error {
	select {
	case <-b.ctx.Done():
		return errors.New("Connection cancelled.")
	// Connect returns before libgm's postConnect sends the active-session RPC.
	// Only a successful List response is reported as Connected in the UI.
	case <-time.After(4 * time.Second):
		return nil
	}
}
func (b *Bridge) ExportSession() (string, error) {
	if !b.client.IsLoggedIn() {
		return "", errors.New("No pairing to save.")
	}
	b.client.AuthData.CookiesLock.RLock()
	defer b.client.AuthData.CookiesLock.RUnlock()
	data, err := json.Marshal(b.client.AuthData)
	if err != nil {
		return "", errors.New("Could not save pairing.")
	}
	return string(data), nil
}
func (b *Bridge) Close() { b.closed.Store(true); b.cancel(); b.client.Disconnect() }

type conversation struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	Kind string `json:"kind"`
}

func (b *Bridge) List() (string, error) {
	if err := b.waitReady(); err != nil {
		return "", err
	}
	resp, err := b.client.ListConversations(100, gmproto.ListConversationsRequest_INBOX)
	if err != nil {
		return "", safeError(err)
	}
	rows := make([]conversation, 0, len(resp.GetConversations()))
	for _, c := range resp.GetConversations() {
		rows = append(rows, conversation{c.GetConversationID(), c.GetName(), c.GetType().String()})
	}
	// Conversation paging isn't implemented in this spike: show the bound.
	data, _ := json.Marshal(map[string]any{"conversations": rows, "bounded": true, "limit": 100})
	return string(data), nil
}

type pageSummary struct {
	IDs          []string `json:"ids"`
	Matches      []string `json:"matches"`
	Cursor       string   `json:"cursor"`
	HasOlder     bool     `json:"hasOlder"`
	SearchTested bool     `json:"searchTested"`
}

func summarize(messages []*gmproto.Message, needle string) pageSummary {
	out := pageSummary{IDs: []string{}, Matches: []string{}, SearchTested: needle != ""}
	seen := map[string]bool{}
	for _, m := range messages {
		id := m.GetMessageID()
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		out.IDs = append(out.IDs, id)
		if needle != "" {
			var body strings.Builder
			for _, part := range m.GetMessageInfo() {
				body.WriteString(part.GetMessageContent().GetContent())
			}
			if strings.Contains(body.String(), needle) {
				out.Matches = append(out.Matches, id)
			}
		}
	}
	return out
}
func (b *Bridge) Read(conversationID, cursorJSON, needle string) (string, error) {
	if conversationID == "" {
		return "", errors.New("Choose a conversation first.")
	}
	if err := b.waitReady(); err != nil {
		return "", err
	}
	var cursor *gmproto.Cursor
	if cursorJSON != "" {
		cursor = &gmproto.Cursor{}
		if protojson.Unmarshal([]byte(cursorJSON), cursor) != nil {
			return "", errors.New("History cursor is invalid. Reload the conversation.")
		}
	}
	resp, err := b.client.FetchMessages(conversationID, 50, cursor)
	if err != nil {
		return "", safeError(err)
	}
	out := summarize(resp.GetMessages(), needle)
	next := resp.GetCursor()
	if next != nil && next.GetLastItemID() != "" && len(out.IDs) > 0 {
		data, _ := protojson.Marshal(next)
		out.Cursor = string(data)
		out.HasOlder = out.Cursor != cursorJSON
	}
	data, _ := json.Marshal(out)
	return string(data), nil
}

type sendDraft struct {
	token   string
	request *gmproto.SendMessageRequest
	expires time.Time
}

func makeSendRequest(c *gmproto.Conversation, text, token string) (*gmproto.SendMessageRequest, []string, error) {
	if strings.TrimSpace(text) == "" || utf8.RuneCountInString(text) > 1600 {
		return nil, nil, errors.New("Enter 1–1600 characters of message text.")
	}
	if c == nil || c.GetConversationID() == "" || c.GetReadOnly() || c.GetDefaultOutgoingID() == "" {
		return nil, nil, errors.New("This conversation is not available for sending.")
	}
	sim := c.GetSimCard().GetSIMData().GetSIMPayload()
	recipients := []string{}
	for _, p := range c.GetParticipants() {
		if p.GetIsMe() || p.GetID().GetParticipantID() == c.GetDefaultOutgoingID() {
			if sim == nil && p.GetID().GetParticipantID() == c.GetDefaultOutgoingID() {
				sim = p.GetSimPayload()
			}
			continue
		}
		number := p.GetID().GetNumber()
		if number == "" {
			number = p.GetFormattedNumber()
		}
		if number == "" {
			return nil, nil, errors.New("Could not verify every recipient. No message sent.")
		}
		name := p.GetFullName()
		if name != "" {
			recipients = append(recipients, name+" · "+number)
		} else {
			recipients = append(recipients, number)
		}
	}
	if sim == nil || len(recipients) == 0 {
		return nil, nil, errors.New("Could not verify the sending SIM or recipients. No message sent.")
	}
	req := &gmproto.SendMessageRequest{ConversationID: c.GetConversationID(), SIMPayload: sim, TmpID: token,
		MessagePayload: &gmproto.MessagePayload{TmpID: token, TmpID2: token, ConversationID: c.GetConversationID(), ParticipantID: c.GetDefaultOutgoingID(),
			MessageInfo: []*gmproto.MessageInfo{{Data: &gmproto.MessageInfo_MessageContent{MessageContent: &gmproto.MessageContent{Content: text}}}}}}
	return req, recipients, nil
}

// PrepareSend performs only reads. It freezes the exact recipient/message preview.
func (b *Bridge) PrepareSend(conversationID, text string) (string, error) {
	b.draftMu.Lock()
	b.draft = nil
	b.draftMu.Unlock()
	if b.closed.Load() || conversationID == "" {
		return "", errors.New("Connect and choose a conversation first.")
	}
	c, err := b.client.GetConversation(conversationID)
	if err != nil {
		return "", safeError(err)
	}
	if c.GetConversationID() != conversationID {
		return "", errors.New("Conversation changed. Choose it again.")
	}
	raw := make([]byte, 16)
	if _, err = rand.Read(raw); err != nil {
		return "", errors.New("Could not prepare the message.")
	}
	token := hex.EncodeToString(raw)
	req, recipients, err := makeSendRequest(c, text, token)
	if err != nil {
		return "", err
	}
	b.draftMu.Lock()
	b.draft = &sendDraft{token: token, request: req, expires: time.Now().Add(2 * time.Minute)}
	b.draftMu.Unlock()
	data, _ := json.Marshal(map[string]any{"token": token, "name": c.GetName(), "recipients": recipients, "text": text, "kind": c.GetType().String()})
	return string(data), nil
}
func (b *Bridge) takeDraft(token string) (*gmproto.SendMessageRequest, error) {
	b.draftMu.Lock()
	defer b.draftMu.Unlock()
	if b.closed.Load() || b.draft == nil || b.draft.token != token || time.Now().After(b.draft.expires) {
		return nil, errors.New("Send confirmation expired. Review the message again.")
	}
	req := b.draft.request
	b.draft = nil
	return req, nil
}

// A confirmation can submit at most once. Never automatically retry a send.
func (b *Bridge) Send(token string) (string, error) {
	req, err := b.takeDraft(token)
	if err != nil {
		return "", err
	}
	resp, err := b.client.SendMessage(req)
	if err != nil {
		return "", errors.New("Send outcome unknown. Check Google Messages before trying again; this app will not retry.")
	}
	if resp.GetStatus() != gmproto.SendMessageResponse_SUCCESS {
		return "", errors.New("Google Messages did not accept the send. Check the conversation before trying again.")
	}
	return "Google Messages accepted the message. Confirm delivery in Google Messages or with the recipient.", nil
}
