// SPDX-License-Identifier: AGPL-3.0-or-later
package pairbridge

import (
	"errors"
	"go.mau.fi/mautrix-gmessages/pkg/libgm/gmproto"
	"strings"
	"testing"
	"time"
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
