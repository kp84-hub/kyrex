package tui

import (
	"fmt"
	"strings"
)

// cellBuffer tracks what's currently rendered on screen
// Only emits ANSI escape codes for cells that actually changed
type cellBuffer struct {
	cells  [][]rune
	width  int
	height int
}

func newCellBuffer(width, height int) *cellBuffer {
	cells := make([][]rune, height)
	for i := range cells {
		cells[i] = make([]rune, width)
		for j := range cells[i] {
			cells[i][j] = ' '
		}
	}
	return &cellBuffer{cells: cells, width: width, height: height}
}

func (b *cellBuffer) diff(newScreen string) string {
	lines := strings.Split(newScreen, "\n")
	var out strings.Builder

	for row, line := range lines {
		if row >= b.height {
			break
		}
		runes := []rune(line)
		changed := false
		for col := 0; col < b.width; col++ {
			var ch rune = ' '
			if col < len(runes) {
				ch = runes[col]
			}
			if b.cells[row][col] != ch {
				if !changed {
					// Move cursor to changed position
					fmt.Fprintf(&out, "\033[%d;%dH", row+1, col+1)
					changed = true
				}
				out.WriteRune(ch)
				b.cells[row][col] = ch
			}
		}
	}
	return out.String()
}
