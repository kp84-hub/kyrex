// SPDX-License-Identifier: AGPL-3.0-or-later
package pairbridge

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.mau.fi/mautrix-gmessages/pkg/libgm/events"
	"go.mau.fi/mautrix-gmessages/pkg/libgm/gmproto"
)

func message(id, text string) *gmproto.Message {
	return &gmproto.Message{MessageID: id, MessageInfo: []*gmproto.MessageInfo{{Data: &gmproto.MessageInfo_MessageContent{MessageContent: &gmproto.MessageContent{Content: text}}}}}
}
func TestSummaryDeduplicatesAndMatchesLiteralText(t *testing.T) {
	s := summarize([]*gmproto.Message{message("1", "RCS [test]"), message("1", "RCS [test]"), message("2", "RCS test")}, "[test]")
	if len(s.IDs) != 2 || len(s.Matches) != 1 || s.Matches[0] != "1" {
		t.Fatalf("unexpected summary: %+v", s)
	}
}
func TestEmptySearchIsNotProofOfHistory(t *testing.T) {
	s := summarize([]*gmproto.Message{message("1", "hello")}, "")
	if s.SearchTested || len(s.Matches) != 0 {
		t.Fatal("empty search must be untested")
	}
}
func TestErrorsDoNotExposeCredentials(t *testing.T) {
	secret := "SID=private-cookie message=private-body"
	if safeError(errors.New(secret)).Error() == secret {
		t.Fatal("raw error leaked")
	}
}

func TestRejectedSessionsStopRecoveryButNetworkErrorsDoNot(t *testing.T) {
	for _, err := range []error{
		fmt.Errorf("refresh: %w", events.ErrInvalidCredentials),
		events.HTTPError{Resp: &http.Response{StatusCode: 401}},
		events.HTTPError{Resp: &http.Response{StatusCode: 403}},
	} {
		if !sessionRejected(err) {
			t.Fatal("invalid session must require sign-in")
		}
	}
	for _, err := range []error{context.DeadlineExceeded, errors.New("offline"), events.HTTPError{Resp: &http.Response{StatusCode: 503}}} {
		if sessionRejected(err) {
			t.Fatal("transient failure must retain saved pairing")
		}
	}
}

func sendConversation() *gmproto.Conversation {
	return &gmproto.Conversation{ConversationID: "c1", DefaultOutgoingID: "me", Participants: []*gmproto.Participant{
		{IsMe: true, ID: &gmproto.SmallInfo{ParticipantID: "me"}, SimPayload: &gmproto.SIMPayload{SIMNumber: 1}},
		{FullName: "Test contact", ID: &gmproto.SmallInfo{ParticipantID: "them", Number: "+15555550123"}},
	}}
}
func TestSendPreviewPreservesExactTextAndRecipients(t *testing.T) {
	c := sendConversation()
	c.Participants = append(c.Participants, &gmproto.Participant{ID: &gmproto.SmallInfo{ParticipantID: "other", Number: "+15555550124"}})
	body := "  Hello 🌎\nsecond line  "
	req, to, err := makeSendRequest(c, body, "txn")
	if err != nil || len(to) != 2 || req.GetConversationID() != "c1" || req.GetMessagePayload().GetParticipantID() != "me" || req.GetSIMPayload().GetSIMNumber() != 1 || req.GetMessagePayload().GetMessageInfo()[0].GetMessageContent().GetContent() != body {
		t.Fatalf("preview/request mismatch: %v", err)
	}
	if req.GetTmpID() != req.GetMessagePayload().GetTmpID() || req.GetTmpID() != req.GetMessagePayload().GetTmpID2() {
		t.Fatal("transaction IDs differ")
	}
}
func TestUnsafeSendTargetsAndBodiesRejected(t *testing.T) {
	for _, mutate := range []func(*gmproto.Conversation){func(c *gmproto.Conversation) { c.ReadOnly = true }, func(c *gmproto.Conversation) { c.DefaultOutgoingID = "" }, func(c *gmproto.Conversation) { c.Participants[0].SimPayload = nil }, func(c *gmproto.Conversation) { c.Participants[1].ID.Number = "" }, func(c *gmproto.Conversation) { c.Participants = c.Participants[:1] }} {
		c := sendConversation()
		mutate(c)
		if _, _, err := makeSendRequest(c, "test", "txn"); err == nil {
			t.Fatal("unsafe target accepted")
		}
	}
	for _, body := range []string{"", "  ", strings.Repeat("x", 1601)} {
		if _, _, err := makeSendRequest(sendConversation(), body, "txn"); err == nil {
			t.Fatal("invalid text accepted")
		}
	}
}
func TestSendConfirmationIsSingleUseAndExpires(t *testing.T) {
	b := &Bridge{draft: &sendDraft{token: "t", request: &gmproto.SendMessageRequest{}, expires: time.Now().Add(time.Minute)}}
	if _, err := b.takeDraft("wrong"); err == nil {
		t.Fatal("wrong confirmation accepted")
	}
	if _, err := b.takeDraft("t"); err != nil {
		t.Fatal(err)
	}
	if _, err := b.takeDraft("t"); err == nil {
		t.Fatal("duplicate send accepted")
	}
	b.draft = &sendDraft{token: "expired", expires: time.Now().Add(-time.Second)}
	if _, err := b.takeDraft("expired"); err == nil {
		t.Fatal("expired confirmation accepted")
	}
}

func TestSendWaitIsBoundedWithoutRetry(t *testing.T) {
	var calls atomic.Int32
	release := make(chan struct{})
	_, err := boundedCall(context.Background(), 5*time.Millisecond, func() (string, error) { calls.Add(1); <-release; return "late", nil })
	close(release)
	if !errors.Is(err, context.DeadlineExceeded) || calls.Load() != 1 {
		t.Fatal("must return timeout and never retry")
	}
}

func TestSnapshotTextSenderTimestampAndProtocol(t *testing.T) {
	c := sendConversation()
	c.Name = "Test group"
	c.Type = gmproto.ConversationType_RCS
	m := message("1", "hello 🌎")
	m.ParticipantID = "them"
	m.Timestamp = 1790935200000000
	row, ok := snapshotRow(c, m)
	if !ok || row.Body != "hello 🌎" || row.Sender != "Test contact" || row.Number != "+15555550123" || row.Direction != "incoming" || row.Kind != "RCS" || row.Received != "2026-10-02T10:00:00Z" {
		t.Fatalf("unexpected snapshot row: %+v", row)
	}
	m.ParticipantID = "me"
	row, ok = snapshotRow(c, m)
	if !ok || row.Sender != "You" || row.Direction != "outgoing" {
		t.Fatal("outgoing message mislabeled")
	}
	m.ParticipantID = "missing"
	row, _ = snapshotRow(c, m)
	if row.Direction != "unknown" || row.Sender != "" {
		t.Fatal("unknown sender guessed")
	}
	m.ConversationID = "another"
	if _, ok = snapshotRow(c, m); ok {
		t.Fatal("cross-conversation message accepted")
	}
	m.ConversationID = ""
	m.MessageInfo = nil
	if _, ok = snapshotRow(c, m); ok {
		t.Fatal("attachment-only row accepted")
	}
	m = message("long", strings.Repeat("🌎", 10001))
	if _, ok = snapshotRow(c, m); ok {
		t.Fatal("oversized row accepted")
	}
}

func TestCloudAndPhoneDraftsAreSeparateAndSingleUse(t *testing.T) {
	b := &Bridge{draft: &sendDraft{token: "local", request: &gmproto.SendMessageRequest{ConversationID: "local"}, expires: time.Now().Add(time.Minute)}, remoteDraft: &sendDraft{token: "remote", request: &gmproto.SendMessageRequest{ConversationID: "remote"}, expires: time.Now().Add(time.Minute)}}
	if _, err := b.consumeDraft("local", true); err == nil {
		t.Fatal("local token accepted by cloud")
	}
	req, err := b.consumeDraft("remote", true)
	if err != nil || req.GetConversationID() != "remote" {
		t.Fatal("cloud draft mismatch")
	}
	if _, err = b.consumeDraft("remote", true); err == nil {
		t.Fatal("cloud draft sent twice")
	}
	req, err = b.takeDraft("local")
	if err != nil || req.GetConversationID() != "local" {
		t.Fatal("local draft consumed by cloud")
	}
	b.remoteDraft = &sendDraft{token: "expired", expires: time.Now().Add(-time.Second)}
	if _, err = b.consumeDraft("expired", true); err == nil {
		t.Fatal("expired cloud draft accepted")
	}
}

func TestCloudSendRechecksGroupAndSIM(t *testing.T) {
	c := sendConversation()
	req, recipients, err := makeSendRequest(c, "text", "tmp_000000000123")
	if err != nil {
		t.Fatal(err)
	}
	draft := &sendDraft{token: "token", request: req, recipients: recipients}
	if !draftStillMatches(c, draft) {
		t.Fatal("unchanged recipients rejected")
	}
	for _, mutate := range []func(*gmproto.Conversation){
		func(c *gmproto.Conversation) {
			c.Participants = append(c.Participants, &gmproto.Participant{ID: &gmproto.SmallInfo{ParticipantID: "new", Number: "+15555550199"}})
		},
		func(c *gmproto.Conversation) { c.Participants[1].ID.Number = "+15555550199" },
		func(c *gmproto.Conversation) { c.Participants[0].SimPayload = &gmproto.SIMPayload{SIMNumber: 2} },
		func(c *gmproto.Conversation) { c.ReadOnly = true },
	} {
		changed := sendConversation()
		mutate(changed)
		if draftStillMatches(changed, draft) {
			t.Fatal("changed target/SIM accepted")
		}
	}
}

func submittedMessage(req *gmproto.SendMessageRequest) *gmproto.Message {
	return &gmproto.Message{MessageID: "server-id", TmpID: req.GetTmpID(), ConversationID: req.GetConversationID(), ParticipantID: req.GetMessagePayload().GetParticipantID(), MessageInfo: req.GetMessagePayload().GetMessageInfo(), MessageStatus: &gmproto.MessageStatus{Status: gmproto.MessageStatusType_OUTGOING_COMPLETE}}
}
func TestSendEchoMustMatchTransactionThreadSenderAndBody(t *testing.T) {
	req, _, _ := makeSendRequest(sendConversation(), "Exact body", "tmp_000000000123")
	for _, mutate := range []func(*gmproto.Message){
		func(m *gmproto.Message) { m.TmpID = "another" },
		func(m *gmproto.Message) { m.ConversationID = "another" },
		func(m *gmproto.Message) { m.ParticipantID = "them" },
		func(m *gmproto.Message) { m.MessageID = "" },
		func(m *gmproto.Message) { m.MessageInfo = message("x", "Other body").MessageInfo },
	} {
		m := submittedMessage(req)
		mutate(m)
		if isSubmittedMessage(req, m) {
			t.Fatal("unrelated echo matched")
		}
	}
	m := submittedMessage(req)
	if !isSubmittedMessage(req, m) {
		t.Fatal("valid echo rejected")
	}
	text, err := verifySubmitted(context.Background(), req, func() (*gmproto.ListMessagesResponse, error) {
		return &gmproto.ListMessagesResponse{Messages: []*gmproto.Message{m}}, nil
	})
	if err != nil || !strings.Contains(text, "Verified") {
		t.Fatal("completed echo not verified", err)
	}
}
func TestSendAcknowledgementWithoutCompletedEchoIsUnverified(t *testing.T) {
	req, _, _ := makeSendRequest(sendConversation(), "body", "tmp_000000000123")
	for _, status := range []gmproto.MessageStatusType{gmproto.MessageStatusType_OUTGOING_SENDING, gmproto.MessageStatusType_OUTGOING_FAILED_GENERIC, gmproto.MessageStatusType_INCOMING_COMPLETE} {
		ctx, cancel := context.WithCancel(context.Background())
		m := submittedMessage(req)
		m.MessageStatus.Status = status
		_, err := verifySubmitted(ctx, req, func() (*gmproto.ListMessagesResponse, error) {
			cancel()
			return &gmproto.ListMessagesResponse{Messages: []*gmproto.Message{m}}, nil
		})
		if err == nil {
			t.Fatal("pending, failed, or incoming echo falsely verified")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	_, err := verifySubmitted(ctx, req, func() (*gmproto.ListMessagesResponse, error) { cancel(); return nil, errors.New("private error") })
	if err == nil || strings.Contains(err.Error(), "private error") {
		t.Fatal("missing echo accepted or raw error exposed")
	}
}
