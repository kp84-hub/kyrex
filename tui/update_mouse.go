package tui

import (
	"strings"
	"time"

	"github.com/atotto/clipboard"
	tea "github.com/charmbracelet/bubbletea"
)

// handleMouseMsg processes all mouse input for selection and textarea interaction.
// Returns (model, cmd, handled) where handled=true means the caller should return immediately.
func (m Model) handleMouseMsg(msg tea.MouseMsg) (Model, tea.Cmd, bool) {
	// Dismiss the command picker on any mouse interaction.
	if m._cmdPickerActive {
		m.closeCommandPicker()
	}

	// Only handle selection during MOUSE mode (full UI) with left button
	if !m.MouseEnabled {
		return m, nil, false
	}

	// Calculate viewport screen origin (matches view.go layout)
	layout := m.recalculateLayout()
	vpStartX := 0
	if layout.ShowSidebar {
		vpStartX = layout.SidebarWidth + 1
	}
	// Viewport starts at top of terminal (Y=0)
	vpStartY := 0

	// --- TEXTAREA ZONE: click/drag in the input box copies its contents ---
	taHeight := m.Textarea.Height()
	taTopY := m.Height - layout.FooterHeight - layout.ContextBarH - taHeight
	inTextarea := msg.Y >= taTopY && msg.Y < taTopY+taHeight

	if inTextarea {
		return m.handleTextareaMouse(msg)
	}

	// --- VIEWPORT ZONE: text selection ---
	// Convert screen coordinates to viewport-local (visible line indices)
	// Subtract 1 from X to account for viewportStyle left padding (Padding(0,1))
	localX := msg.X - vpStartX - 1
	localY := msg.Y - vpStartY // visible line index (0 = top of viewport)

	if msg.Button == tea.MouseButtonLeft {
					switch msg.Action {
			case tea.MouseActionPress:
				if localX >= 0 && localX < m.Viewport.Width &&
					localY >= 0 && localY < m.Viewport.Height {
					m.Selecting = true
					// Convert viewport-relative coordinates to absolute line indices
					absLine := localY + m.Viewport.YOffset
					m.SelectStart = SelectionPoint{Line: absLine, Col: localX}
					m.SelectEnd = SelectionPoint{Line: absLine, Col: localX}
					m.AutoScrollDir = 0
				}
			case tea.MouseActionMotion:
				if m.Selecting {
					// Clamp to viewport bounds
					if localX < 0 {
						localX = 0
					}
					if localX >= m.Viewport.Width {
						localX = m.Viewport.Width - 1
					}
					if localY < 0 {
						localY = 0
					}
					if localY >= m.Viewport.Height {
						localY = m.Viewport.Height - 1
					}
					// Convert viewport-relative coordinates to absolute line indices
					absLine := localY + m.Viewport.YOffset
					m.SelectEnd = SelectionPoint{Line: absLine, Col: localX}

				// Regenerate viewport content with selection highlights
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))

				// Auto-scroll edge detection
				if localY >= m.Viewport.Height-1 && !m.Viewport.AtBottom() {
					m.AutoScrollDir = 1 // scroll down
				} else if localY <= 0 && m.Viewport.YOffset > 0 {
					m.AutoScrollDir = -1 // scroll up
				} else {
					m.AutoScrollDir = 0
				}
			}
		default: // Release or other action
			if m.Selecting {
				m.Selecting = false
				m.AutoScrollDir = 0
				selectedText := m.GetSelectedText()
				if selectedText != "" {
					clipboard.WriteAll(selectedText)
					m.Toast = "Copied to clipboard"
					m.ToastEnd = time.Now().Add(2 * time.Second)
				}
			}
		}
	} else if m.Selecting {
		// Any non-left button cancels selection
		m.Selecting = false
		m.AutoScrollDir = 0
	}

	// Wheel events — let them fall through to the viewport's own scroll handler
	if msg.Button == tea.MouseButtonWheelUp || msg.Button == tea.MouseButtonWheelDown {
		return m, nil, false
	}

	return m, nil, true
}

// handleTextareaMouse handles mouse events in the textarea zone.
func (m Model) handleTextareaMouse(msg tea.MouseMsg) (Model, tea.Cmd, bool) {
	if msg.Button == tea.MouseButtonLeft {
		switch msg.Action {
		case tea.MouseActionPress:
			m._textareaDrag = true
		default: // Release or other
			if m._textareaDrag {
				m._textareaDrag = false
				val := strings.TrimSpace(m.Textarea.Value())
				if val != "" {
					clipboard.WriteAll(val)
					m.Toast = "Input copied to clipboard"
					m.ToastEnd = time.Now().Add(2 * time.Second)
				}
			}
		}
	} else if m._textareaDrag {
		m._textareaDrag = false
	}
	return m, nil, true
}
