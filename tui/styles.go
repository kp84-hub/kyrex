package tui

import "github.com/charmbracelet/lipgloss"

var (
	// Terminal-safe foreground colors (no backgrounds — inherit terminal theme)
	fg        = lipgloss.Color("#ffffff")
	accent    = lipgloss.Color("#7aa2f7")
	purple    = lipgloss.Color("#bb9af7")
	green     = lipgloss.Color("#9ece6a")
	red       = lipgloss.Color("#f7768e")
	orange    = lipgloss.Color("#e0af68")
	yellow    = lipgloss.Color("#e5c07b")
	border    = lipgloss.Color("#3d3d5c")
	subtle    = lipgloss.Color("#9aa5ce")
	thinkingC = lipgloss.Color("33")
	// Muted accent colors for sidebar section headers
	greenMuted = lipgloss.Color("#4caf50") // muted green for WORKSPACE
	purpleSoft = lipgloss.Color("#9c7ecf") // soft purple for SESSION
	amberMuted = lipgloss.Color("#d19a66") // muted amber for EXECUTION TIMELINE
	cyanDim    = lipgloss.Color("#4db5bd") // dim cyan for "> You" label
	darkgrey   = lipgloss.Color("#666666") // neutral dark grey for secondary body text

	// Tool state colors (muted, systems-oriented)
	toolQueued  = subtle
	toolRunning = accent
	toolSuccess = green
	toolWarning = orange
	toolBlocked = yellow
	toolFailed  = red

	// Tool state icons
	toolIconQueued  = "○"
	toolIconRunning = "⟳"
	toolIconSuccess = "✓"
	toolIconWarning = "⚠"
	toolIconBlocked = "◌"
	toolIconFailed  = "✗"

	thinkingStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder(), false, false, false, true).
			BorderForeground(lipgloss.Color("33")).
			Padding(0, 1).
			MarginBottom(1).
			Foreground(thinkingC).
			Italic(true)

	separatorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("244")).
			Bold(true).
			MarginTop(1).
			MarginBottom(1)

	// Styles (no Background() — transparent, inherits terminal theme)
	sidebarStyle = lipgloss.NewStyle().
			Border(lipgloss.DoubleBorder(), false, true, false, false).
			BorderForeground(border).
			Padding(1)

	// Sidebar section header styles (each with distinct muted accent color)
	sidebarActiveHeaderStyle = lipgloss.NewStyle().
					Foreground(accent).
					Bold(true).
					MarginBottom(1).
					Underline(true)

	sidebarWorkspaceHeaderStyle = lipgloss.NewStyle().
					Foreground(greenMuted).
					Bold(true).
					MarginBottom(1).
					Underline(true)

	sidebarSessionHeaderStyle = lipgloss.NewStyle().
					Foreground(purpleSoft).
					Bold(true).
					MarginBottom(1).
					Underline(true)

	sidebarTimelineHeaderStyle = lipgloss.NewStyle().
					Foreground(amberMuted).
					Bold(true).
					MarginBottom(1).
					Underline(true)

	sidebarHeaderStyle = lipgloss.NewStyle().
				Foreground(accent).
				Bold(true).
				MarginBottom(1).
				Underline(true)

	viewportStyle = lipgloss.NewStyle().
			Padding(0, 1)

	viewportBorderStyle = lipgloss.NewStyle().
			Border(lipgloss.NormalBorder(), false, false, false, true).
			BorderForeground(accent).
			Padding(0, 1)

	textareaStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("240")).
			Padding(0, 1).
			PaddingLeft(1)

	// Textarea style without left border (for when sidebar is shown)
	textareaStyleNoLeft = lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder(), false, true, true, true).
				BorderForeground(lipgloss.Color("240")).
				Padding(0, 1).
				PaddingLeft(1)

	// Textarea with focused border (accent) vs blurred border (border/dim)
	textareaFocusedStyle = textareaStyle.Copy().
				BorderForeground(accent)

	textareaBlurredStyle = textareaStyle.Copy().
				BorderForeground(border)

	// Subtle horizontal rule separator above the input textarea
	inputSeparatorStyle = lipgloss.NewStyle().
				Foreground(border)

	footerStyle = lipgloss.NewStyle().
			Foreground(fg).
			Height(1).
			Padding(0, 1)

	phaseStyle = lipgloss.NewStyle().
			Foreground(purple).
			Padding(0, 1).
			Bold(true).
			MarginRight(1)

	// Pill-style status badges
	pillBase = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(border).
			Padding(0, 1)

	pillOnline = pillBase.Copy().
			Foreground(green).
			BorderForeground(green)

	pillOffline = pillBase.Copy().
			Foreground(red).
			BorderForeground(red)

	pillPhase = pillBase.Copy().
			Foreground(purple).
			BorderForeground(purple)

	timerStyle = lipgloss.NewStyle().
			Foreground(subtle).
			Italic(true)

	toolTraceStyle = lipgloss.NewStyle().
			Foreground(subtle).
			Italic(true).
			MarginLeft(2)

	logoStyle = lipgloss.NewStyle().
			Foreground(accent).
			Padding(0, 1).
			Bold(true).
			MarginBottom(1)

	brandStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#00ffff")).
			Bold(true).
			MarginLeft(1)

	contextStyle = lipgloss.NewStyle().
			Foreground(subtle).
			Italic(true).
			Padding(0, 1)

	toastStyle = lipgloss.NewStyle().
			Foreground(fg).
			Bold(true).
			Padding(0, 2).
			MarginBottom(1)

	overviewStyle = lipgloss.NewStyle().
			Foreground(fg).
			MarginTop(1)

	missionSummaryStyle = lipgloss.NewStyle().
				Foreground(subtle).
				Padding(1, 1).
				Border(lipgloss.RoundedBorder(), false, false, false, false).
				BorderForeground(border).
				MarginTop(1)

	telemetryStyle = lipgloss.NewStyle().
			Foreground(subtle).
			Padding(0, 1)
)
