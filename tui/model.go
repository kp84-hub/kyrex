package tui

import (
	"encoding/json"
	"os"
	"time"

	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/viewport"
	"github.com/charmbracelet/lipgloss"
	"github.com/kp84-hub/kx/internal/race"
	"github.com/kp84-hub/kx/internal/rift"
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
	case ExecNodePending:
		return "pending"
	case ExecNodeRunning:
		return "running"
	case ExecNodeSuccess:
		return "success"
	case ExecNodeWarning:
		return "warning"
	case ExecNodeBlocked:
		return "blocked"
	case ExecNodeFailed:
		return "failed"
	default:
		return "unknown"
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

// ── Sidebar Model ──

type SidebarModel struct {
	Collapsed         bool
	Width             int
	ShowSettings      bool

	// Settings state
	ModelList         []string
	CurrentModel      string
	ProviderList      []string
	CurrentProvider   string

	// Status
	EngineStatus      string // "online", "offline", "busy"
	TokenCount        int
	PromptTokens      int
	CompletionTokens  int

	// Tool call expand/collapse
	ExpandedTools     map[string]bool

	// Scroll button
	ShowScrollBtn     bool

	// Attach file
	AttachFilePath    string

	// Generation state
	IsGenerating      bool
}

func NewSidebarModel() SidebarModel {
	return SidebarModel{
		Collapsed:         false,
		Width:             28,
		ShowSettings:      false,
		EngineStatus:      "online",
		ModelList:         []string{},
		CurrentModel:      "unknown",
		ProviderList:      []string{},
		CurrentProvider:   "unknown",
		ExpandedTools:     make(map[string]bool),
		ShowScrollBtn:     false,
	}
}

// Turn represents a complete conversation turn with all its components
type Turn struct {
	UserMessage   string
	Thinking      string
	ToolCalls     []ToolCall
	Diffs         []components.DiffBlock
	Response      string
	Logs          []string
}

// ToolCall represents a single tool invocation within a turn
type ToolCall struct {
	Name   string
	Args   string
	Result string
	State  ToolState
}

type Model struct {
	Phase       Phase
	History     []string
	Turns       []Turn
	CurrentTurn *Turn
	CurrToken   string
	Reasoning   string
	Context     string
	LLMInfo     string
	Timer       int
	_timerActive bool // starts counting on first prompt submission
	AutoApprove bool // when true, pending confirm gates auto-approve after AutoApproveDelay
	AutoApproveDelay time.Duration // how long to wait before auto-approving
	ScrollLock  bool

	Viewport    viewport.Model
	Textarea    textarea.Model

	Width       int
	Height      int

	// Sidebar data
	WorkspaceDirs  []string
	WorkspaceFiles []string
	ActiveFiles    []string

	// Session context
	SessionBranch string

	// Copy-on-write workspace for sandboxed file edits
	Workspace    *rift.Workspace
	WorkspaceMgr *rift.Manager   // for MergeBack/Discard

	// Mission summary
	MissionSummary string

	// Tool telemetry (ring buffer, replaces CurrentTool/ToolResult)
	Tools       ToolTelemetry

	// Execution tree
	ExecTree    *ExecutionTree

	// Stream tracking
	IsSending   bool
	_sendingTick int // animation frame for "Sending..." dots
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
	ConfirmType string // "" = edit (side-by-side diff), "deletion" = single-box proposal

	// Execution Timeline
	Timeline *components.ExecutionTimeline

	// Custom Selection Engine (viewport-relative indexing)
	Selecting      bool
	SelectStart    SelectionPoint // visible line + col (screen coords)
	SelectEnd      SelectionPoint // visible line + col (screen coords)
	AutoScrollDir  int

	// Viewport throttle
	_lastViewportFlush   time.Time
	_viewportDirty       bool
	_tokenCoalescePending bool // true while a 16ms coalesce tick is in flight

	// Viewport content cache
	_cachedViewportContent string
	_cachedWidth           int

	// History rendering cache (separate from full viewport cache)
	_cachedHistoryContent string
	_cachedHistoryLines   int
	_cachedHistoryWidth   int
	_historyCacheValid    bool

	// Incremental rendering: stable history cache
	// Only the dynamic tail (reasoning/tokens) changes during streaming.
	// The completed history above is stable and cached until it changes.
	_stableHistoryContent string
	_stableHistoryLines   int
	_stableHistoryLen     int // len(m.History) when cache was built
	_stableHistoryWidth   int // viewport width when cache was built

	// Last content string passed to Viewport.SetContent — skip redundant calls
	_lastSetContent string

	// Phase event tracking for timeline
	_phasePlanID string
	_phaseExecID string
	_lastToolID  string

	// Progress update counter for collapsed narration in scrollback
	_progressUpdateCount int

	// Approval result deduplication (collapses identical lines with ×N)
	_lastApprovalLine string
	_approvalCount    int

	// Engine message suppression
	_suppressEngine bool

	// Paste burst detection
	_lastKeyTime time.Time

	// Textarea mouse drag
	_textareaDrag bool

	// Usage stats overlay
	_usageOverlayActive bool
	_usageStats          map[string]interface{}

	// Model picker overlay
	_interruptPending    bool
	_modelPickerActive   bool
	_modelPickerLoading  bool
	_modelPickerAllItems []string
	_modelPickerItems    []string
	_modelPickerCurrent  string
	_modelPickerFilter   string
	_modelPickerInput    string
	_modelPickerIndex    int

	// Command picker overlay
	_cmdPickerActive  bool
	_cmdPickerItems   []string
	_cmdPickerIndex   int
	_cmdPickerInput   string

	// ── NEW: Sidebar Component ──
	Sidebar     SidebarModel

	// ── Split Pane: Diff Blocks (rendered in top Reasoning Pane) ──
	DiffBlocks        []components.DiffBlock
	ActiveDiffID      string
	ReasoningDone     bool          // true when final reasoning is committed to history

	// ── Layout dimension tracking (prevents redundant SetWidth/SetHeight calls) ──
	_lastAppliedVpWidth    int
	_lastAppliedVpHeight   int
	_lastAppliedTaWidth    int
	_lastAppliedTaHeight   int
	_lastAppliedShowSidebar bool
	_lastAppliedLayout     Layout // cached layout for View() to avoid recalculation

	// ── Textarea height debounce (prevents rapid dirty flag toggling) ──
	_pendingTaHeight int
	_taHeightDebounce time.Time

	// ── Render metrics (diagnostic instrumentation) ──
	_metrics *RenderMetrics

	// ── Static UI caches (sidebar/footer don't change while typing) ──
	_cachedSidebar       string
	_cachedSidebarKey    string // key representing sidebar state
	_cachedFooter        string
	_cachedFooterKey     string // key representing footer state

	// ── Race Mode ──
	RaceMode           bool
	Race               *race.Race
	_raceConfirmPending bool
	_raceConfirmTask   string
	_raceConfirmModels []string
	_raceStartTime     time.Time
	_raceTaskSent      map[int]bool // per-lane task-send guard keyed by LaneID

	// ── Race Comparing State (post-completion, pre-merge) ──
	_raceComparing    bool           // true = comparing/merge selection mode
	_raceHighlight    int            // highlighted lane index
	_raceViewingDiff  int            // lane index whose diff is shown (-1 = table)
	_raceDiffs        map[int]string // per-lane diff text keyed by LaneID
	_raceDiffLines    map[int]int    // per-lane diff line count
	_raceDiffScroll   int            // scroll offset for diff view
	_raceMergePending bool           // true while merge cmd is in flight

	// ── Race Gate State ──
	_raceGates         map[int]bool   // lane ID → passed (true) or failed (false)
	_raceGateOutput    map[int]string // lane ID → truncated gate output
	_raceGatesRunning  bool           // true while gates are being computed
	_raceViewingGate   int            // lane index whose gate output is shown (-1 = not viewing)
	_raceGateScroll    int            // scroll offset for gate output view

	// ── Race Wizard State ──
	_raceWizardStep    int    // 0 = inactive, 1 = awaiting task, 2 = awaiting models
	_raceWizardTask    string // accumulated task text during wizard

	// ── Race Model Picker (multi-select) ──
	_raceModelPickerActive   bool
	_raceModelPickerLoading  bool
	_raceModelPickerAll      []string
	_raceModelPickerItems    []string
	_raceModelPickerFilter   string
	_raceModelPickerIndex    int
	_raceModelPickerSelected []string // ordered, max 4

	// ── Consult Mode ──
	_consultActive    bool
	_consult          *race.Race
	_consultTaskSent  map[int]bool

	// ── Consult Wizard State ──
	_consultWizardStep int    // 0 = inactive, 1 = awaiting focus, 2 = awaiting models
	_consultWizardTask string // accumulated focus text during wizard

	// ── Consult Model Picker (max 2) ──
	_consultModelPickerActive   bool
	_consultModelPickerLoading  bool
	_consultModelPickerAll      []string
	_consultModelPickerItems    []string
	_consultModelPickerFilter   string
	_consultModelPickerIndex    int
	_consultModelPickerSelected []string

	// ── Consult Confirm ──
	_consultConfirmPending bool
	_consultConfirmModels  []string
	_consultConfirmFocus   string

	// ── Setup Flow State ──
	_setupActive       bool     // true when setup flow is active
	_setupStep         int      // 0=provider, 1=api_key, 2=model, 3=test, 4=save
	_setupOllama       bool     // true when Ollama (local) preset is selected
	_setupProvider     string   // selected provider
	_setupBaseURL     string   // API base URL
	_setupAPIKey      string   // API key input
	_setupAPIKeyEnv   string   // env var name (if using env var)
	_setupModel       string   // selected model
	_setupModels      []string // full fetched models list
	_setupFilteredModels []string // filtered models based on text input
	_setupModelFilter string   // text filter for model picker
	_setupCustomModel bool     // true when user is typing custom model name
	_setupHeaders     string   // custom headers
	_setupTestResult  string   // connection test result
	_setupTestPassed bool     // connection test status
	_setupSaving     bool     // true while saving
	_setupError      string   // error message if any
	_setupInput      string   // current text input
	_setupCursorPos  int      // cursor position in text input
}

type SelectionPoint struct {
	Line int
	Col  int
}

// Layout holds all computed dimensions for the TUI layout.
type Layout struct {
	ShowSidebar    bool
	SidebarWidth   int
	MainWidth      int
	TextareaHeight int
	ViewportHeight int
	ViewportWidth  int
	ContextBarH    int // 1 if no sidebar, 0 if sidebar
	FooterHeight   int
}

// recalculateLayout computes all layout dimensions from current state.
// This is the single source of truth for layout math — used by View(),
// WindowSizeMsg handler, and textarea auto-grow.
func (m *Model) recalculateLayout() Layout {
	showSidebar := m.ShowSidebar
	if m.ConfirmID != "" {
		showSidebar = false
	}

	sidebarWidth := 0
	if showSidebar {
		sidebarWidth = 25
		if sidebarWidth > m.Width/3 {
			sidebarWidth = m.Width / 3
		}
	}

	mainWidth := m.Width - sidebarWidth - 1
	if !showSidebar {
		mainWidth = m.Width
	}
	if mainWidth < 1 {
		mainWidth = 1
	}

	footerHeight := 1
	contextBarH := 0
	if !showSidebar {
		contextBarH = 1
	}

	// Fixed textarea height of 1 to prevent layout shifts and flickering
	lineCount := 1

	// Border adds 2 rows (top + bottom)
	textareaBorderH := 2
	viewportHeight := m.Height - lineCount - textareaBorderH - footerHeight - contextBarH
	if viewportHeight < 1 {
		viewportHeight = 1
	}

	vpW := mainWidth - 2
	if vpW < 1 {
		vpW = 1
	}

	return Layout{
		ShowSidebar:    showSidebar,
		SidebarWidth:   sidebarWidth,
		MainWidth:      mainWidth,
		TextareaHeight: lineCount,
		ViewportHeight: viewportHeight,
		ViewportWidth:  vpW,
		ContextBarH:    contextBarH,
		FooterHeight:   footerHeight,
	}
}

// applyLayout pushes computed layout dimensions into the actual components.
// Only calls setters when values actually changed — avoids triggering
// bubbletea internal recalculations and viewport content re-wrapping.
func (m *Model) applyLayout(l Layout) {
	taWidth := l.MainWidth - 2

	if l.ViewportWidth != m._lastAppliedVpWidth {
		m.Viewport.Width = l.ViewportWidth
		m._lastAppliedVpWidth = l.ViewportWidth
	}
	if l.ViewportHeight != m._lastAppliedVpHeight {
		m.Viewport.Height = l.ViewportHeight
		m._lastAppliedVpHeight = l.ViewportHeight
	}
	if taWidth != m._lastAppliedTaWidth {
		m.Textarea.SetWidth(taWidth)
		m._lastAppliedTaWidth = taWidth
	}
	if l.TextareaHeight != m._lastAppliedTaHeight {
		m.Textarea.SetHeight(l.TextareaHeight)
		m._lastAppliedTaHeight = l.TextareaHeight
	}
	m._lastAppliedShowSidebar = l.ShowSidebar
	m._lastAppliedLayout = l // cache for View() to use
}

// getProvider, getAPIKey, getBaseURL read provider config from ~/.px/config.json
// and fall back to environment variables.
func loadPXConfig() (provider, apiKeyEnv, apiKey, baseURL string) {
	path := os.Getenv("HOME") + "/.px/config.json"
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var cfg struct {
		Provider   string `json:"provider"`
		APIKey    string `json:"api_key"`
		APIKeyEnv string `json:"api_key_env"`
		BaseURL   string `json:"base_url"`
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		return
	}
	provider = cfg.Provider
	apiKeyEnv = cfg.APIKeyEnv
	apiKey = cfg.APIKey
	baseURL = cfg.BaseURL
	return
}

// loadWorkspaceConfig reads .px/config.json from the workspace Source directory,
// falling back to the global ~/.px/config.json if no workspace is set.
func loadWorkspaceConfig(m *Model) (provider, apiKey, baseURL string) {
	path := ""
	if m.Workspace != nil && m.Workspace.Source != "" {
		path = m.Workspace.Source + "/.px/config.json"
	}
	if path == "" {
		path = os.Getenv("HOME") + "/.px/config.json"
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var cfg struct {
		Provider   string `json:"provider"`
		APIKey    string `json:"api_key"`
		APIKeyEnv string `json:"api_key_env"`
		BaseURL   string `json:"base_url"`
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		return
	}
	provider = cfg.Provider
	apiKey = cfg.APIKey
	if cfg.APIKeyEnv != "" {
		if env := os.Getenv(cfg.APIKeyEnv); env != "" {
			apiKey = env
		}
	}
	baseURL = cfg.BaseURL
	return
}

func (m *Model) getProvider() string {
	if m.Sidebar.CurrentProvider != "unknown" && m.Sidebar.CurrentProvider != "" {
		return m.Sidebar.CurrentProvider
	}
	p, _, _, _ := loadPXConfig()
	if p != "" {
		return p
	}
	return os.Getenv("KYREX_PROVIDER")
}

func (m *Model) getAPIKey() string {
	if k := os.Getenv("KYREX_API_KEY"); k != "" {
		return k
	}
	_, env, k, _ := loadPXConfig()
	if env != "" {
		return os.Getenv(env)
	}
	if k != "" {
		return k
	}
	return os.Getenv("OPENAI_API_KEY")
}

func (m *Model) getBaseURL() string {
	if u := os.Getenv("KYREX_BASE_URL"); u != "" {
		return u
	}
	_, _, _, u := loadPXConfig()
	if u != "" {
		return u
	}
	return os.Getenv("OPENAI_BASE_URL")
}

func NewModel(sendFunc func(interface{}) error) Model {
	ta := textarea.New()
	ta.Placeholder = "Type a prompt..."
	ta.Focus()
	ta.CharLimit = 10000
	ta.ShowLineNumbers = false
	ta.SetHeight(1)
	ta.MaxHeight = 6
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
		Timeline:        components.NewExecutionTimeline(200),
		Sidebar:         NewSidebarModel(),
		_metrics:        NewRenderMetrics(),
		_raceHighlight:    0,
		_raceViewingDiff:  -1,
		_raceDiffs:        make(map[int]string),
		_raceDiffLines:    make(map[int]int),
		_raceGates:        make(map[int]bool),
		_raceGateOutput:   make(map[int]string),
		_raceViewingGate:  -1,
		_progressUpdateCount: 0,
		_approvalCount:       0,
	}
}
