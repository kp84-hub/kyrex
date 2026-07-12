package tui

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// sessionMessage represents a single message in the lane session JSON.
type sessionMessage struct {
	Role      string            `json:"role"`
	Content   string            `json:"content"`
	ToolCalls []sessionToolCall `json:"tool_calls,omitempty"`
}

// sessionToolCall represents a tool invocation within a session message.
type sessionToolCall struct {
	Name      string                 `json:"name"`
	Arguments map[string]interface{} `json:"arguments,omitempty"`
}

// sessionData represents the .px_sessions/main.json structure.
type sessionData struct {
	History []sessionMessage `json:"history"`
}

// extractLaneStory reads a lane's session file at sessionPath (the path to
// .px_sessions/main.json) and produces a compact chronological digest of the
// lane's work log. It is capped at maxBytes (default 4096). If maxBytes <= 0,
// the default cap is used.
//
// For each assistant message with tool_calls, one line per call is emitted:
//
//	→ toolname: <first 80 chars of key arg>
//
// For tool-result messages, only failures/errors are noted:
//
//	✗ toolname: <first 100 chars>
//
// Assistant text content is included trimmed to first 150 chars per message.
// System messages are skipped entirely.
//
// If the output exceeds maxBytes, the earliest and latest portions are kept
// with a "[... N rounds omitted ...]" marker in the middle.
func extractLaneStory(sessionPath string, maxBytes int) (string, error) {
	if maxBytes <= 0 {
		maxBytes = 4096
	}

	data, err := os.ReadFile(sessionPath)
	if err != nil {
		return "", fmt.Errorf("read session file: %w", err)
	}

	var sd sessionData
	if err := json.Unmarshal(data, &sd); err != nil {
		return "", fmt.Errorf("parse session JSON: %w", err)
	}

	if len(sd.History) == 0 {
		return "", nil
	}

	// Build digest lines.
	var lines []string
	roundCount := 0

	for msgIdx, msg := range sd.History {
		role := strings.TrimSpace(msg.Role)

		// Skip system messages entirely.
		if role == "system" {
			continue
		}

		if role == "assistant" {
			// Include text content (first 150 chars).
			text := strings.TrimSpace(msg.Content)
			if text != "" {
				if len(text) > 150 {
					text = text[:147] + "..."
				}
				lines = append(lines, text)
			}

			// One line per tool call.
			for _, tc := range msg.ToolCalls {
				argPreview := firstKeyArg(tc.Arguments, 80)
				lines = append(lines, fmt.Sprintf("→ %s: %s", tc.Name, argPreview))
			}

			if len(msg.ToolCalls) > 0 || text != "" {
				roundCount++
			}
			continue
		}

		if role == "tool" {
			// Only note failures/errors.
			content := strings.TrimSpace(msg.Content)
			if isErrorContent(content) {
				// Try to infer the tool name from preceding assistant messages.
				toolName := inferToolName(sd.History, msgIdx)
				truncated := content
				if len(truncated) > 100 {
					truncated = truncated[:97] + "..."
				}
				lines = append(lines, fmt.Sprintf("✗ %s: %s", toolName, truncated))
			}
			continue
		}

		// For user messages, just count them (not included verbatim to save space).
		if role == "user" {
			roundCount++
			continue
		}
	}

	if len(lines) == 0 {
		return "", nil
	}

	// Join with newlines.
	full := strings.Join(lines, "\n")

	// If within cap, return as-is.
	if len(full) <= maxBytes {
		return full, nil
	}

	// Over cap: keep earliest and latest portions with an omission marker.
	return truncateEarliestLatest(lines, maxBytes, roundCount), nil
}

// firstKeyArg extracts the first argument value from a tool call's arguments
// map, truncated to maxLen characters. It prefers common key names like
// "path", "file_path", "file", "content", "command", "query", "code", "url".
func firstKeyArg(args map[string]interface{}, maxLen int) string {
	if len(args) == 0 {
		return "(no args)"
	}

	// Priority key order.
	priorityKeys := []string{"path", "file_path", "file", "content", "command", "query", "code", "url", "name", "target", "destination", "source", "pattern", "text"}

	for _, key := range priorityKeys {
		if val, ok := args[key]; ok {
			s := fmt.Sprintf("%v", val)
			s = strings.TrimSpace(s)
			if len(s) > maxLen {
				s = s[:maxLen-3] + "..."
			}
			if s != "" {
				return s
			}
		}
	}

	// Fallback: use the first value regardless of key.
	for _, val := range args {
		s := fmt.Sprintf("%v", val)
		s = strings.TrimSpace(s)
		if len(s) > maxLen {
			s = s[:maxLen-3] + "..."
		}
		if s != "" {
			return s
		}
		break
	}

	return "(no args)"
}

// isErrorContent returns true if the content indicates a failure/error.
func isErrorContent(content string) bool {
	if content == "" {
		return false
	}
	lower := strings.ToLower(content)
	errorIndicators := []string{
		"error", "fail", "exception", "panic", "traceback",
		"not found", "permission denied", "timeout", "exit status",
		"could not", "unable to", "cannot", "invalid",
	}
	for _, ind := range errorIndicators {
		if strings.Contains(lower, ind) {
			return true
		}
	}
	return false
}

// inferToolName looks backward from the given tool result message to find the
// preceding assistant message that likely invoked the tool.
func inferToolName(history []sessionMessage, toolIdx int) string {
	// Walk backward from the tool message to find the most recent assistant
	// message with tool_calls.
	for i := toolIdx - 1; i >= 0; i-- {
		if history[i].Role == "assistant" && len(history[i].ToolCalls) > 0 {
			// Return the name of the first (or only) tool call.
			return history[i].ToolCalls[0].Name
		}
		// Stop if we hit another tool result (we walked past the pairing).
		if history[i].Role == "tool" {
			break
		}
	}
	return "tool"
}

// truncateEarliestLatest keeps the earliest and latest portions of the digest
// with a "[... N rounds omitted ...]" marker in the middle.
func truncateEarliestLatest(lines []string, maxBytes int, totalRounds int) string {
	// We keep roughly the first 40% and last 40% of the byte budget,
	// reserving ~200 bytes for the omission marker.
	halfBudget := (maxBytes - 200) / 2
	if halfBudget < 100 {
		halfBudget = 100
	}

	var earliest, latest []string
	earliestSize := 0
	latestSize := 0

	// Build earliest portion (top of lines).
	for _, line := range lines {
		if earliestSize+len(line)+1 > halfBudget {
			break
		}
		earliest = append(earliest, line)
		earliestSize += len(line) + 1
	}

	if len(earliest) == 0 && len(lines) > 0 {
		first := lines[0]
		if len(first) > halfBudget {
			first = first[:halfBudget-3] + "..."
		}
		earliest = append(earliest, first)
	}

	// Build latest portion (bottom of lines).
	for i := len(lines) - 1; i >= 0; i-- {
		if latestSize+len(lines[i])+1 > halfBudget {
			break
		}
		latest = append([]string{lines[i]}, latest...)
		latestSize += len(lines[i]) + 1
	}

	// Count how many rounds are omitted between earliest and latest.
	omittedRounds := totalRounds
	if len(earliest) > 0 || len(latest) > 0 {
		omittedRounds = totalRounds - 1 // rough estimate; good enough for marker
		if omittedRounds < 1 {
			omittedRounds = 1
		}
	}

	omittedMarker := fmt.Sprintf("[... %d rounds omitted ...]", omittedRounds)

	// Combine: earliest + marker + latest.
	var sb strings.Builder
	for _, line := range earliest {
		sb.WriteString(line)
		sb.WriteString("\n")
	}
	sb.WriteString(omittedMarker)
	sb.WriteString("\n")
	for _, line := range latest {
		sb.WriteString(line)
		sb.WriteString("\n")
	}

	result := sb.String()
	if len(result) > maxBytes {
		result = result[:maxBytes]
	}
	return result
}

// normalizeSessionPath returns the path to the lane's session file.
// laneDir is the clone directory of the lane.
func normalizeSessionPath(laneDir string) string {
	return filepath.Join(laneDir, ".px_sessions", "main.json")
}
