// SPDX-License-Identifier: AGPL-3.0-or-later
package pairbridge
import("errors";"testing";"go.mau.fi/mautrix-gmessages/pkg/libgm/gmproto")
func message(id,text string)*gmproto.Message{return &gmproto.Message{MessageID:id,MessageInfo:[]*gmproto.MessageInfo{{Data:&gmproto.MessageInfo_MessageContent{MessageContent:&gmproto.MessageContent{Content:text}}}}}}
func TestSummaryDeduplicatesAndMatchesLiteralText(t *testing.T){s:=summarize([]*gmproto.Message{message("1","RCS [test]"),message("1","RCS [test]"),message("2","RCS test")},"[test]");if len(s.IDs)!=2||len(s.Matches)!=1||s.Matches[0]!="1"{t.Fatalf("unexpected summary: %+v",s)}}
func TestEmptySearchIsNotProofOfHistory(t *testing.T){s:=summarize([]*gmproto.Message{message("1","hello")},"");if s.SearchTested||len(s.Matches)!=0{t.Fatal("empty search must be untested")}}
func TestErrorsDoNotExposeCredentials(t *testing.T){secret:="SID=private-cookie message=private-body";if safeError(errors.New(secret)).Error()==secret{t.Fatal("raw error leaked")}}
