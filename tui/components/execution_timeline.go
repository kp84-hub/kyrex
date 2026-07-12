package components

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

type EventType string

const (
	EventPlan      EventType = "PLAN"
	EventTool      EventType = "TOOL"
	EventApproval  EventType = "APPROVAL"
	EventExecution EventType = "EXECUTION"
	EventError     EventType = "ERROR"
)

type EventStatus string

const (
	StatusPending  EventStatus = "pending"
	StatusRunning  EventStatus = "running"
	StatusSuccess  EventStatus = "success"
	StatusWarning  EventStatus = "warning"
	StatusRejected EventStatus = "rejected"
	StatusFailed   EventStatus = "failed"
)

type TimelineEvent struct {
	ID         string
	ParentID   string
	Timestamp  time.Time
	Type       EventType
	Status     EventStatus
	Title      string
	Details    string
	DurationMs int
}

type ExecutionTimeline struct {
	Events     []TimelineEvent
	EventIndex map[string]int
	MaxEvents  int

	cachedContent string
	cachedWidth   int
	cachedMaxRows int
	dirty         bool
}

func NewExecutionTimeline(capacity int) *ExecutionTimeline {
	return &ExecutionTimeline{
		Events:     make([]TimelineEvent, 0, capacity),
		EventIndex: make(map[string]int, capacity),
		MaxEvents:  capacity,
		dirty:      true,
	}
}

func (t *ExecutionTimeline) Add(event TimelineEvent) *TimelineEvent {
	if event.ID == "" {
		event.ID = fmt.Sprintf("%d", time.Now().UnixNano())
	}
	if event.Timestamp.IsZero() {
		event.Timestamp = time.Now()
	}

	if len(t.Events) >= t.MaxEvents {
		old := t.Events[0]
		delete(t.EventIndex, old.ID)
		t.Events = t.Events[1:]
		for k, v := range t.EventIndex {
			t.EventIndex[k] = v - 1
		}
	}

	idx := len(t.Events)
	t.Events = append(t.Events, event)
	t.EventIndex[event.ID] = idx
	t.dirty = true
	return &t.Events[idx]
}

func (t *ExecutionTimeline) UpdateByID(id string, status EventStatus, details string) *TimelineEvent {
	idx, ok := t.EventIndex[id]
	if !ok {
		return nil
	}

	e := &t.Events[idx]
	e.Status = status
	if details != "" {
		e.Details = details
	}
	if e.DurationMs == 0 {
		e.DurationMs = int(time.Since(e.Timestamp).Milliseconds())
	}
	t.dirty = true
	return e
}

func (t *ExecutionTimeline) Clear() {
	t.Events = t.Events[:0]
	t.EventIndex = make(map[string]int, t.MaxEvents)
	t.cachedContent = ""
	t.dirty = true
}

func (t *ExecutionTimeline) EventsForCurrentTurn() []TimelineEvent {
	if len(t.Events) == 0 {
		return nil
	}
	out := make([]TimelineEvent, len(t.Events))
	copy(out, t.Events)
	return out
}

func (t *ExecutionTimeline) ToolCounts() map[string]int {
	counts := make(map[string]int)
	for _, e := range t.Events {
		if e.Type == EventTool && e.Status == StatusSuccess {
			counts[e.Title]++
		}
	}
	return counts
}

// Render renders the execution timeline with newest-first windowing and
// pinned live entries. maxRows is the number of ENTRY rows available.
// If maxRows <= 0, renders nothing.
func (t *ExecutionTimeline) Render(width, maxRows int) string {
	if len(t.Events) == 0 || maxRows <= 0 {
		return ""
	}

	if !t.dirty && t.cachedWidth == width && t.cachedMaxRows == maxRows && t.cachedContent != "" {
		return t.cachedContent
	}

	usableWidth := width - 2
	if usableWidth < 12 {
		usableWidth = 12
	}

	// Determine prefix width: MM:SS (5) + space (1) + icon (1) + space (1) = 8
	prefixWidth := 8
	// Duration field max: 5 chars (e.g. "1.2ms", "22.3s", "1.2m ")
	durationWidth := 5
	titleMax := usableWidth - prefixWidth - durationWidth - 1
	if titleMax < 3 {
		titleMax = 3
		durationWidth = usableWidth - prefixWidth - titleMax - 1
		if durationWidth < 1 {
			durationWidth = 1
		}
	}

	// Separate live (pending/running) and completed/failed events
	var live, completed []TimelineEvent
	for _, e := range t.Events {
		if e.Status == StatusPending || e.Status == StatusRunning {
			live = append(live, e)
		} else {
			completed = append(completed, e)
		}
	}

	budget := maxRows
	liveCount := len(live)

	// If live entries exceed budget, truncate to newest live + marker
	liveOmitted := 0
	if liveCount > budget {
		liveOmitted = liveCount - budget
		live = live[liveOmitted:]
		liveCount = len(live)
	}

	remaining := budget - liveCount

	// Determine completed entries to show and whether a marker is needed
	completedToShow := len(completed)
	markerNeeded := false

	if remaining <= 0 {
		completedToShow = 0
		markerNeeded = len(completed) > 0 || liveOmitted > 0
	} else if len(completed) > remaining {
		// Reserve one row for the marker
		completedToShow = remaining - 1
		if completedToShow < 0 {
			completedToShow = 0
		}
		markerNeeded = true
	} else {
		completedToShow = len(completed)
		markerNeeded = liveOmitted > 0
	}

	// If all events fit, suppress marker
	totalVisible := liveCount + completedToShow
	if !markerNeeded && totalVisible >= len(t.Events) && liveOmitted == 0 {
		markerNeeded = false
	}

	// Build output: [marker] + [newest completed, chronological] + [live, chronological]
	var sb strings.Builder
	var prevGroup EventType

	// Marker line
	if markerNeeded {
		omitted := len(t.Events) - totalVisible
		if omitted < 0 {
			omitted = 0
		}
		markerLine := lipgloss.NewStyle().Foreground(subtleT).Render(fmt.Sprintf("… %d earlier", omitted))
		sb.WriteString(markerLine)
		sb.WriteString("\n")
		prevGroup = "" // reset group context after marker
	}

	// Completed entries (newest = tail of slice, in chronological order)
	completedStart := len(completed) - completedToShow
	if completedStart < 0 {
		completedStart = 0
	}
	for _, e := range completed[completedStart:] {
		prevGroup = writeEvent(&sb, e, usableWidth, prefixWidth, durationWidth, titleMax, prevGroup)
	}

	// Live entries (chronological, pinned at bottom)
	for _, e := range live {
		prevGroup = writeEvent(&sb, e, usableWidth, prefixWidth, durationWidth, titleMax, prevGroup)
	}

	t.cachedContent = sb.String()
	t.cachedWidth = width
	t.cachedMaxRows = maxRows
	t.dirty = false

	return t.cachedContent
}

// writeEvent renders a single timeline event line into sb, handling group headers.
// Returns the new prevGroup value.
func writeEvent(sb *strings.Builder, e TimelineEvent, usableWidth, prefixWidth, durationWidth, titleMax int, prevGroup EventType) EventType {
	group := deriveGroup(e, prevGroup)
	if group != "" && group != prevGroup && group != EventTool && group != EventError {
		if prevGroup != "" {
			sb.WriteString("\n")
		}
		prevGroup = group
		sb.WriteString(groupHeaderStyle.Render(string(group)))
		sb.WriteString("\n")
	} else if group != "" {
		prevGroup = group
	}

	ts := e.Timestamp.Format("15:04")
	icon, color := iconForStatus(e.Status)
	title := e.Title
	if len(title) > titleMax {
		title = title[:titleMax-1] + "…"
	}
	dur := formatDuration(e.DurationMs)

	leftPart := lipgloss.NewStyle().Foreground(subtleT).Render(ts) + " " +
		lipgloss.NewStyle().Foreground(color).Render(icon) + " " +
		lipgloss.NewStyle().Foreground(fgT).Render(title)

	leftLen := lipgloss.Width(leftPart)
	paddingNeeded := usableWidth - leftLen - len(dur)
	if paddingNeeded < 1 {
		paddingNeeded = 1
	}

	durPart := strings.Repeat(" ", paddingNeeded) + lipgloss.NewStyle().Foreground(subtleT).Render(dur)

	sb.WriteString(leftPart)
	sb.WriteString(durPart)
	sb.WriteString("\n")
	return prevGroup
}

func deriveGroup(e TimelineEvent, prevGroup EventType) EventType {
	switch e.Type {
	case EventPlan:
		return EventPlan
	case EventApproval:
		return EventApproval
	case EventExecution:
		return EventExecution
	case EventError:
		return prevGroup
	case EventTool:
		return prevGroup
	default:
		return prevGroup
	}
}

func iconForStatus(status EventStatus) (string, lipgloss.Color) {
	switch status {
	case StatusPending:
		return "◻", subtleT
	case StatusRunning:
		return "⧗", accentT
	case StatusSuccess:
		return "✓", greenT
	case StatusWarning, StatusRejected:
		return "⚠", orangeT
	case StatusFailed:
		return "✗", redT
	default:
		return "◻", subtleT
	}
}

func formatDuration(ms int) string {
	if ms == 0 {
		return ""
	}
	d := time.Duration(ms) * time.Millisecond
	switch {
	case d < time.Second:
		return fmt.Sprintf("%dms", ms)
	case d < time.Minute:
		return fmt.Sprintf("%.1fs", d.Seconds())
	default:
		return fmt.Sprintf("%.1fm", d.Minutes())
	}
}

var (
	subtleT = lipgloss.Color("#9aa5ce")
	accentT = lipgloss.Color("#7aa2f7")
	greenT  = lipgloss.Color("#9ece6a")
	orangeT = lipgloss.Color("#e0af68")
	redT    = lipgloss.Color("#f7768e")
	fgT     = lipgloss.Color("#ffffff")

	groupHeaderStyle = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#bb9af7")).
				Bold(true).
				MarginTop(1).
				MarginBottom(0)
)
