package tui

import (
	"github.com/charmbracelet/lipgloss"
)

// ── Additional Colors (view.go defines the core palette) ──
var (
	fgDim      = lipgloss.Color("#565f89")
	bg         = lipgloss.Color("#1a1b26") // terminal bg, used only where needed
	selection  = lipgloss.Color("#3d59a1")

	accentAlt  = lipgloss.Color("#2ac3de")
	teal       = lipgloss.Color("#1abc9c")
	pink       = lipgloss.Color("#ff007c")

	// Status indicators (use view.go colors)
	statusOnline  = green
	statusOffline = red
	statusBusy    = yellow

	// Tokens
	codeBg    = lipgloss.Color("#1d1f2e")
	codeBorder = lipgloss.Color("#2f3346")
)

// ── Component Styles ──

// Sidebar
var (
	sidebarSection = lipgloss.NewStyle().
			Foreground(accent).
			Bold(true).
			MarginTop(1).
			MarginBottom(0).
			Padding(0, 0)

	sidebarLabel = lipgloss.NewStyle().
			Foreground(fgDim).
			Padding(0, 0)

	sidebarValue = lipgloss.NewStyle().
			Foreground(fg).
			Padding(0, 0)
)

// Status Bar
var (
	statusBarStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder(), true, true, true, true).
			BorderForeground(border).
			Padding(0, 1).
			MarginBottom(1)

	statusDotStyle = lipgloss.NewStyle().
			Bold(true).
			MarginRight(1)
)

// Chat / Viewport
var (
	userBubbleStyle = lipgloss.NewStyle().
			Foreground(fg).
			Padding(0, 1).
			Border(lipgloss.RoundedBorder(), false, false, false, true).
			BorderForeground(accent).
			MarginBottom(1).
			MarginLeft(4)

	assistantBubbleStyle = lipgloss.NewStyle().
				Foreground(fg).
				Padding(0, 1).
				Border(lipgloss.RoundedBorder(), false, false, false, true).
				BorderForeground(purple).
				MarginBottom(1)

	toolCallBubbleStyle = lipgloss.NewStyle().
				Foreground(fgDim).
				Padding(0, 1).
				Background(codeBg).
				MarginBottom(0).
				MarginLeft(2)

	toolResultPreviewStyle = lipgloss.NewStyle().
				Foreground(fgDim).
				Padding(0, 1).
				Background(lipgloss.Color("#16161e")).
				MarginBottom(1).
				MarginLeft(4)

	codeBlockStyle = lipgloss.NewStyle().
			Background(codeBg).
			Padding(0, 1).
			MarginBottom(1).
			MarginLeft(2).
			Border(lipgloss.RoundedBorder(), true, true, true, true).
			BorderForeground(codeBorder)

	codeHeaderStyle = lipgloss.NewStyle().
			Foreground(fgDim).
			Background(codeBg).
			Padding(0, 0).
			MarginBottom(0)
)

// Input Area
var (
	inputBarStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder(), true, true, true, true).
			BorderForeground(border).
			Padding(0, 1).
			MarginTop(1)

	btnPrimaryStyle = lipgloss.NewStyle().
			Foreground(bg).
			Background(accent).
			Bold(true).
			Padding(0, 1).
			MarginLeft(1)

	btnDangerStyle = lipgloss.NewStyle().
			Foreground(bg).
			Background(red).
			Bold(true).
			Padding(0, 1).
			MarginLeft(1)

	btnOutlineStyle = lipgloss.NewStyle().
			Foreground(accent).
			Border(lipgloss.RoundedBorder(), true, true, true, true).
			BorderForeground(accent).
			Padding(0, 1).
			MarginLeft(1)

	modeBtnActive = lipgloss.NewStyle().
			Foreground(bg).
			Background(purple).
			Bold(true).
			Padding(0, 1).
			MarginLeft(1)

	modeBtnInactive = lipgloss.NewStyle().
			Foreground(fgDim).
			Border(lipgloss.RoundedBorder(), true, true, true, true).
			BorderForeground(border).
			Padding(0, 1).
			MarginLeft(1)

	attachBtnStyle = lipgloss.NewStyle().
			Foreground(fgDim).
			Padding(0, 1).
			MarginLeft(1)
)

// Settings Panel
var (
	settingsPanelStyle = lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder(), true, true, true, true).
				BorderForeground(border).
				Padding(0, 1).
				MarginTop(1)

	settingsHeaderStyle = lipgloss.NewStyle().
				Foreground(accent).
				Bold(true).
				Padding(0, 0).
				MarginBottom(1)

	settingsLabelStyle = lipgloss.NewStyle().
				Foreground(fgDim).
				Width(12)

	settingsValueStyle = lipgloss.NewStyle().
				Foreground(fg)

	dropdownStyle = lipgloss.NewStyle().
			Foreground(accent).
			Border(lipgloss.RoundedBorder(), true, true, true, true).
			BorderForeground(border).
			Padding(0, 1)

	dropdownItemStyle = lipgloss.NewStyle().
				Foreground(fg).
				Padding(0, 1)

	dropdownSelectedStyle = lipgloss.NewStyle().
				Foreground(bg).
				Background(accent).
				Padding(0, 1)
)

// Scroll to bottom
var (
	scrollBtnStyle = lipgloss.NewStyle().
			Foreground(accent).
			Background(lipgloss.Color("#2f3346")).
			Bold(true).
			Padding(0, 1).
			MarginBottom(1)
)

// Message header labels
var (
	userLabelStyle = lipgloss.NewStyle().
			Foreground(accent).
			Bold(true).
			MarginRight(1)

	assistantLabelStyle = lipgloss.NewStyle().
				Foreground(purple).
				Bold(true).
				MarginRight(1)

	systemLabelStyle = lipgloss.NewStyle().
				Foreground(orange).
				Bold(true).
				MarginRight(1)

	thinkingLabelStyle = lipgloss.NewStyle().
				Foreground(thinkingC).
				Italic(true).
				MarginRight(1)
)

// Overlay
var (
	overlayTitleStyle = lipgloss.NewStyle().
				Foreground(accent).
				Bold(true).
				Padding(1, 2)
)

// Misc
var (
)
