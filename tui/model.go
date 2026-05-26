package tui

import (
	"time"

	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/viewport"
	"github.com/charmbracelet/lipgloss"
	"github.com/kp84-hub/kx/tui/components"
)

type ToolState string

const (
	ToolStateQueued   ToolState = "queued"
	ToolStateRunning  ToolState = "running"
	ToolStateSuccess  ToolState = "success"
	ToolStateWarning  ToolState = "warning"
	ToolStateBlocked  ToolState = "blocked"
	ToolStateFailed   ToolState = "failed"
)

type ToolEvent struct {
	ID        string
	Name      string
	Args      string
	Result    string
	State     ToolState
	StartTime time.Time
	EndTime   time.Time
}

func (e ToolEvent) Duration() time.Duration {
	if e.EndTime.IsZero() {
		return time.Since(e.StartTime)
	}
	return e.EndTime.Sub(e.StartTime)
}

type ToolTelemetry struct {
	events    []ToolEvent
	ringIndex int
	size      int
	capacity  int
}

func NewToolTelemetry(capacity int) ToolTelemetry {
	return ToolTelemetry{
		events:   make([]ToolEvent, capacity),
		capacity: capacity,
	}
}

func (t *ToolTelemetry) Add(event ToolEvent) {
	t.events[t.ringIndex] = event
	t.ringIndex = (t.ringIndex + 1) % t.capacity
	if t.size < t.capacity {
		t.size++
	}
}

func (t ToolTelemetry) Recent() []ToolEvent {
	if t.size == 0 {
		return nil
	}
	out := make([]ToolEvent, t.size)
	for i := 0; i < t.size; i++ {
		idx := (t.ringIndex - t.size + i + t.capacity) % t.capacity
		out[i] = t.events[idx]
	}
	return out
}

func (t ToolTelemetry) Latest() *ToolEvent {
	if t.size == 0 {
		return nil
	}
	idx := (t.ringIndex - 1 + t.capacity) % t.capacity
	return &t.events[idx]
}

func (t *ToolTelemetry) UpdateLast(state ToolState, result string) {
	if t.size == 0 {
		return
	}
	idx := (t.ringIndex - 1 + t.capacity) % t.capacity
	t.events[idx].State = state
	t.events[idx].EndTime = time.Now()
	if result != "" {
		t.events[idx].Result = result
	}
}

type Phase string

const (
	PhaseBooting Phase = "BOOTING"
	PhaseIdle    Phase = "IDLE"
	PhasePlan    Phase = "PLAN"
	PhaseExecute Phase = "EXECUTE"
)

type ExecNodeState int

const (
	ExecNodePending   ExecNodeState = iota
	ExecNodeRunning
	ExecNodeSuccess
	ExecNodeWarning
	ExecNodeBlocked
	ExecNodeFailed
)

func (s ExecNodeState) String() string {
	switch s {
	case ExecNodePending:  return "pending"
	case ExecNodeRunning:  return "running"
	case ExecNodeSuccess:  return "success"
	case ExecNodeWarning:  return "warning"
	case ExecNodeBlocked:  return "blocked"
	case ExecNodeFailed:   return "failed"
	default:               return "unknown"
	}
}

type ExecNode struct {
	Label    string
	State    ExecNodeState
	Children []*ExecNode
	active   bool
}

type ExecutionTree struct {
	Root     *ExecNode
	PlanRoot *ExecNode
	ExecRoot *ExecNode
	Current  *ExecNode
	pending  []*ExecNode
}

func NewExecutionTree() *ExecutionTree {
	root := &ExecNode{Label: "KYREX", State: ExecNodeRunning, active: true}
	return &ExecutionTree{
		Root:     root,
		PlanRoot: &ExecNode{Label: "PLAN", State: ExecNodePending},
		ExecRoot: &ExecNode{Label: "EXECUTION", State: ExecNodePending},
		Current:  root,
	}
}

func (t *ExecutionTree) StartPlan() {
	t.Root.Children = []*ExecNode{t.PlanRoot, t.ExecRoot}
	t.PlanRoot.State = ExecNodeRunning
	t.PlanRoot.active = true
	t.Current = t.PlanRoot
}

func (t *ExecutionTree) AddPlanStep(label string) *ExecNode {
	node := &ExecNode{Label: label, State: ExecNodePending}
	t.PlanRoot.Children = append(t.PlanRoot.Children, node)
	return node
}

func (t *ExecutionTree) StartExecution() {
	t.ExecRoot.State = ExecNodeRunning
	t.ExecRoot.active = true
	t.PlanRoot.State = ExecNodeSuccess
	t.PlanRoot.active = false
	t.Current = t.ExecRoot
}

func (t *ExecutionTree) AddExecStep(label string) *ExecNode {
	node := &ExecNode{Label: label, State: ExecNodePending}
	t.ExecRoot.Children = append(t.ExecRoot.Children, node)
	return node
}

func (t *ExecutionTree) CompleteExecStep(label string, state ExecNodeState) {
	for _, n := range t.ExecRoot.Children {
		if n.Label == label && n.State == ExecNodePending {
			n.State = state
			n.active = false
			return
		}
	}
}

func (t *ExecutionTree) SetExecStepRunning(label string) {
	for _, n := range t.ExecRoot.Children {
		if n.Label == label && n.State == ExecNodePending {
			n.State = ExecNodeRunning
			n.active = true
			t.Current = n
			return
		}
	}
}

func (t *ExecutionTree) Clear() {
	*t = *NewExecutionTree()
}

type Model struct {
	Phase       Phase
	History     []string
	CurrToken   string
	Reasoning   string
	Context     string
	LLMInfo     string
	Timer       int
	ScrollLock  bool

	Viewport    viewport.Model
	Textarea    textarea.Model

	Width       int
	Height      int

	// Sidebar data
	ProjectFiles []string

	// Session context
	SessionBranch string
	Mode          string

	// Mission summary
	MissionSummary string

	// Tool telemetry (ring buffer, replaces CurrentTool/ToolResult)
	Tools       ToolTelemetry

	// Execution tree
	ExecTree    *ExecutionTree

	// Stream tracking
	IsThinking  bool
	CurrentTool string
	ToolArgs    string
	ToolResult  string

	SendFunc    func(interface{}) error
	ShowSidebar bool

	// Mouse toggle
	MouseEnabled bool

	// Toast notification
	Toast    string
	ToastEnd time.Time

	// Confirmation Gate
	ConfirmPath string
	ConfirmDiff string
	ConfirmID   string

	// Execution Timeline
	Timeline *components.ExecutionTimeline

	// Custom Selection Engine (absolute buffer indexing)
	Selecting      bool
	SelectStart    SelectionPoint
	SelectEnd      SelectionPoint
	AutoScrollDir  int  // 1=down, -1=up, 0=none

	// Viewport throttle: prevents O(n) per-token full-history rebuilds
	_lastViewportFlush time.Time
	_viewportDirty     bool

	// Viewport content cache (Phase 6 performance)
	_cachedViewportContent string
	_cachedWidth           int

	// Phase event tracking for timeline
	_phasePlanID string
	_phaseExecID string
	_lastToolID  string

	// Engine message suppression (prevents stale in-flight messages after clear/reset)
	_suppressEngine bool
}

type SelectionPoint struct {
	Line int // absolute line index in the full unpaginated text buffer
	Col  int // column within that line
}

func NewModel(sendFunc func(interface{}) error) Model {
	ta := textarea.New()
	ta.Placeholder = "Type a prompt..."
	ta.Focus()
	ta.CharLimit = 10000 // Handle massive pastes
	ta.ShowLineNumbers = false
	ta.SetHeight(1)
	ta.MaxHeight = 1
	ta.FocusedStyle.Base = lipgloss.NewStyle()
	ta.BlurredStyle.Base = lipgloss.NewStyle()

	vp := viewport.New(0, 0)
	vp.SetContent("Welcome to Kyrex TUI\n")

	return Model{
		Phase:        PhaseBooting,
		Textarea:     ta,
		Viewport:     vp,
		SendFunc:     sendFunc,
		LLMInfo:      "Model: unknown",
		Context:      "No context set",
		ShowSidebar:  true,
		MouseEnabled: true,
		Tools:        NewToolTelemetry(50),
		ExecTree:     NewExecutionTree(),
		Timeline:     components.NewExecutionTimeline(200),
	}
}
