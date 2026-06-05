package components

import (
	"regexp"
	"strconv"
	"strings"
)

// DiffStatus represents the lifecycle state of a diff block
type DiffStatus string

const (
	DiffStatusPending  DiffStatus = "pending"
	DiffStatusApproved DiffStatus = "approved"
	DiffStatusRejected DiffStatus = "rejected"
	DiffStatusApplied  DiffStatus = "applied"
)

// DiffLineType classifies each line in a hunk
type DiffLineType string

const (
	DiffLineContext DiffLineType = "context"
	DiffLineAdd     DiffLineType = "add"
	DiffLineRemove  DiffLineType = "remove"
)

// WordChangeType marks whether a word was added or removed
type WordChangeType string

const (
	WordChangeAdd    WordChangeType = "add"
	WordChangeRemove WordChangeType = "remove"
)

// WordChange represents a highlighted word within a diff line
type WordChange struct {
	Start int
	End   int
	Type  WordChangeType
}

// DiffLine represents a single line in a diff hunk
type DiffLine struct {
	Type         DiffLineType
	OldLineNum   int          // Line number in original file (0 for additions)
	NewLineNum   int          // Line number in new file (0 for deletions)
	Content      string       // The actual line content (without +/- prefix)
	WordChanges  []WordChange // Word-level changes for inline highlighting
	NoNewline    bool         // True if followed by "\ No newline at end of file"
}

// DiffHunk represents a contiguous section of changes
type DiffHunk struct {
	OldStart int        // Starting line in original file
	OldCount int        // Number of lines from original file
	NewStart int        // Starting line in new file
	NewCount int        // Number of lines in new file
	Lines    []DiffLine // All lines in this hunk
}

// DiffBlock represents a complete file diff with metadata
type DiffBlock struct {
	ID        string       // Unique identifier for tracking
	FilePath  string       // Path to the file being changed
	Hunks     []DiffHunk   // All hunks in this diff
	Status    DiffStatus   // Current lifecycle state
	Collapsed bool         // Whether the diff is collapsed in UI
	OldFile   string       // Original file path (from --- line)
	NewFile   string       // New file path (from +++ line)
}

// hunkHeaderRegex matches @@ -oldStart,oldCount +newStart,newCount @@
var hunkHeaderRegex = regexp.MustCompile(`^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@`)

// ParseUnifiedDiff parses a unified diff string into a DiffBlock
// Handles edge cases: missing newlines, empty hunks, malformed input
func ParseUnifiedDiff(diff string, id string) *DiffBlock {
	if diff == "" {
		return &DiffBlock{
			ID:     id,
			Status: DiffStatusPending,
		}
	}

	block := &DiffBlock{
		ID:     id,
		Status: DiffStatusPending,
	}

	lines := strings.Split(diff, "\n")
	var currentHunk *DiffHunk
	var oldLineNum, newLineNum int
	var pendingNoNewline bool

	for i := 0; i < len(lines); i++ {
		line := lines[i]

		// Handle "\ No newline at end of file" marker
		if strings.HasPrefix(line, `\ No newline at end of file`) {
			// Mark the previous line as having no trailing newline
			if currentHunk != nil && len(currentHunk.Lines) > 0 {
				currentHunk.Lines[len(currentHunk.Lines)-1].NoNewline = true
			}
			pendingNoNewline = true
			continue
		}

		// File headers
		if strings.HasPrefix(line, "--- ") {
			block.OldFile = strings.TrimPrefix(line, "--- ")
			// Extract just the filename if it's a path
			if strings.Contains(block.OldFile, "/") {
				parts := strings.Split(block.OldFile, "/")
				block.FilePath = parts[len(parts)-1]
			} else {
				block.FilePath = block.OldFile
			}
			continue
		}

		if strings.HasPrefix(line, "+++ ") {
			block.NewFile = strings.TrimPrefix(line, "+++ ")
			// Use new file path if available (more accurate for renames)
			if block.NewFile != "" && block.NewFile != "/dev/null" {
				if strings.Contains(block.NewFile, "/") {
					parts := strings.Split(block.NewFile, "/")
					block.FilePath = parts[len(parts)-1]
				} else {
					block.FilePath = block.NewFile
				}
			}
			continue
		}

		// Hunk header
		if strings.HasPrefix(line, "@@ ") {
			// Save previous hunk if exists
			if currentHunk != nil {
				block.Hunks = append(block.Hunks, *currentHunk)
			}

			// Parse hunk header
			matches := hunkHeaderRegex.FindStringSubmatch(line)
			if matches == nil {
				// Malformed hunk header, skip
				continue
			}

			currentHunk = &DiffHunk{}
			currentHunk.OldStart, _ = strconv.Atoi(matches[1])
			if matches[2] != "" {
				currentHunk.OldCount, _ = strconv.Atoi(matches[2])
			} else {
				currentHunk.OldCount = 1
			}
			currentHunk.NewStart, _ = strconv.Atoi(matches[3])
			if matches[4] != "" {
				currentHunk.NewCount, _ = strconv.Atoi(matches[4])
			} else {
				currentHunk.NewCount = 1
			}

			oldLineNum = currentHunk.OldStart
			newLineNum = currentHunk.NewStart
			pendingNoNewline = false
			continue
		}

		// Skip index lines and other metadata
		if strings.HasPrefix(line, "index ") || strings.HasPrefix(line, "diff ") ||
			strings.HasPrefix(line, "new file") || strings.HasPrefix(line, "deleted file") ||
			strings.HasPrefix(line, "old mode") || strings.HasPrefix(line, "new mode") {
			continue
		}

		// Diff content lines
		if currentHunk == nil {
			// No hunk started yet, skip
			continue
		}

		if len(line) == 0 {
			// Empty line - treat as context
			diffLine := DiffLine{
				Type:       DiffLineContext,
				OldLineNum: oldLineNum,
				NewLineNum: newLineNum,
				Content:    "",
			}
			currentHunk.Lines = append(currentHunk.Lines, diffLine)
			oldLineNum++
			newLineNum++
			continue
		}

		prefix := line[0]
		content := ""
		if len(line) > 1 {
			content = line[1:]
		}

		switch prefix {
		case '-':
			diffLine := DiffLine{
				Type:       DiffLineRemove,
				OldLineNum: oldLineNum,
				NewLineNum: 0,
				Content:    content,
			}
			currentHunk.Lines = append(currentHunk.Lines, diffLine)
			oldLineNum++

		case '+':
			diffLine := DiffLine{
				Type:       DiffLineAdd,
				OldLineNum: 0,
				NewLineNum: newLineNum,
				Content:    content,
			}
			currentHunk.Lines = append(currentHunk.Lines, diffLine)
			newLineNum++

		case ' ':
			// Context line
			diffLine := DiffLine{
				Type:       DiffLineContext,
				OldLineNum: oldLineNum,
				NewLineNum: newLineNum,
				Content:    content,
			}
			currentHunk.Lines = append(currentHunk.Lines, diffLine)
			oldLineNum++
			newLineNum++

		default:
			// Unknown prefix - treat as context (defensive)
			diffLine := DiffLine{
				Type:       DiffLineContext,
				OldLineNum: oldLineNum,
				NewLineNum: newLineNum,
				Content:    line,
			}
			currentHunk.Lines = append(currentHunk.Lines, diffLine)
			oldLineNum++
			newLineNum++
		}

		// Apply pending "no newline" marker
		if pendingNoNewline && len(currentHunk.Lines) > 0 {
			currentHunk.Lines[len(currentHunk.Lines)-1].NoNewline = true
			pendingNoNewline = false
		}
	}

	// Save final hunk
	if currentHunk != nil {
		block.Hunks = append(block.Hunks, *currentHunk)
	}

	// Compute word-level changes for better highlighting
	block.ComputeWordChanges()

	return block
}

// ComputeWordChanges analyzes each hunk to identify word-level changes
// This enables fine-grained highlighting within changed lines
func (b *DiffBlock) ComputeWordChanges() {
	for hunkIdx := range b.Hunks {
		hunk := &b.Hunks[hunkIdx]
		
		// Find consecutive remove/add pairs
		i := 0
		for i < len(hunk.Lines) {
			line := &hunk.Lines[i]
			
			// Look for a remove followed by an add
			if line.Type == DiffLineRemove && i+1 < len(hunk.Lines) {
				nextLine := &hunk.Lines[i+1]
				if nextLine.Type == DiffLineAdd {
					// Compute word-level diff between these two lines
					removeChanges, addChanges := computeWordDiff(line.Content, nextLine.Content)
					line.WordChanges = removeChanges
					nextLine.WordChanges = addChanges
					i += 2
					continue
				}
			}
			i++
		}
	}
}

// computeWordDiff compares two strings word-by-word and returns change markers
func computeWordDiff(oldStr, newStr string) ([]WordChange, []WordChange) {
	oldWords := tokenizeWords(oldStr)
	newWords := tokenizeWords(newStr)

	// Use simple LCS-based diff
	oldChanges := []WordChange{}
	newChanges := []WordChange{}

	// Build LCS table
	lcs := computeLCS(oldWords, newWords)

	// Trace back to find changes
	oldIdx, newIdx, lcsIdx := 0, 0, 0
	oldPos, newPos := 0, 0

	for oldIdx < len(oldWords) || newIdx < len(newWords) {
		if lcsIdx < len(lcs) && oldIdx < len(oldWords) && oldWords[oldIdx] == lcs[lcsIdx] &&
			newIdx < len(newWords) && newWords[newIdx] == lcs[lcsIdx] {
			// Matching word - no change
			oldPos += len(oldWords[oldIdx])
			newPos += len(newWords[newIdx])
			oldIdx++
			newIdx++
			lcsIdx++
		} else if oldIdx < len(oldWords) && (lcsIdx >= len(lcs) || oldWords[oldIdx] != lcs[lcsIdx]) {
			// Word removed from old
			wordLen := len(oldWords[oldIdx])
			oldChanges = append(oldChanges, WordChange{
				Start: oldPos,
				End:   oldPos + wordLen,
				Type:  WordChangeRemove,
			})
			oldPos += wordLen
			oldIdx++
		} else if newIdx < len(newWords) && (lcsIdx >= len(lcs) || newWords[newIdx] != lcs[lcsIdx]) {
			// Word added in new
			wordLen := len(newWords[newIdx])
			newChanges = append(newChanges, WordChange{
				Start: newPos,
				End:   newPos + wordLen,
				Type:  WordChangeAdd,
			})
			newPos += wordLen
			newIdx++
		}
	}

	return oldChanges, newChanges
}

// tokenizeWords splits a string into word tokens (preserving whitespace)
func tokenizeWords(s string) []string {
	if s == "" {
		return []string{}
	}

	var words []string
	var current strings.Builder
	inWord := false

	for _, ch := range s {
		isSpace := ch == ' ' || ch == '\t'
		
		if isSpace {
			if inWord {
				words = append(words, current.String())
				current.Reset()
				inWord = false
			}
			current.WriteRune(ch)
		} else {
			if !inWord && current.Len() > 0 {
				// Flush whitespace
				words = append(words, current.String())
				current.Reset()
			}
			current.WriteRune(ch)
			inWord = true
		}
	}

	if current.Len() > 0 {
		words = append(words, current.String())
	}

	return words
}

// computeLCS finds the longest common subsequence of two string slices
func computeLCS(a, b []string) []string {
	m, n := len(a), len(b)
	
	// Build DP table
	dp := make([][]int, m+1)
	for i := range dp {
		dp[i] = make([]int, n+1)
	}

	for i := 1; i <= m; i++ {
		for j := 1; j <= n; j++ {
			if a[i-1] == b[j-1] {
				dp[i][j] = dp[i-1][j-1] + 1
			} else {
				if dp[i-1][j] > dp[i][j-1] {
					dp[i][j] = dp[i-1][j]
				} else {
					dp[i][j] = dp[i][j-1]
				}
			}
		}
	}

	// Trace back to get LCS
	lcs := make([]string, 0, dp[m][n])
	i, j := m, n
	for i > 0 && j > 0 {
		if a[i-1] == b[j-1] {
			lcs = append([]string{a[i-1]}, lcs...)
			i--
			j--
		} else if dp[i-1][j] > dp[i][j-1] {
			i--
		} else {
			j--
		}
	}

	return lcs
}

// Summary returns a human-readable summary of the diff
func (b *DiffBlock) Summary() string {
	if b == nil || len(b.Hunks) == 0 {
		return "No changes"
	}

	adds, removes := 0, 0
	for _, hunk := range b.Hunks {
		for _, line := range hunk.Lines {
			switch line.Type {
			case DiffLineAdd:
				adds++
			case DiffLineRemove:
				removes++
			}
		}
	}

	return formatDiffSummary(adds, removes)
}

// formatDiffSummary creates a compact summary string
func formatDiffSummary(adds, removes int) string {
	parts := []string{}
	if adds > 0 {
		parts = append(parts, "+"+strconv.Itoa(adds))
	}
	if removes > 0 {
		parts = append(parts, "-"+strconv.Itoa(removes))
	}
	if len(parts) == 0 {
		return "No changes"
	}
	return strings.Join(parts, " ")
}

// IsEmpty returns true if the diff has no actual changes
func (b *DiffBlock) IsEmpty() bool {
	if b == nil {
		return true
	}
	for _, hunk := range b.Hunks {
		for _, line := range hunk.Lines {
			if line.Type == DiffLineAdd || line.Type == DiffLineRemove {
				return false
			}
		}
	}
	return true
}
