package tui

import (
	"strings"
	"testing"
)

func TestNormalFooterHint(t *testing.T) {
	tests := []struct {
		name       string
		width      int
		want       []string
		notWant    []string
	}{
		{
			name:    "narrow terminal",
			width:   59,
			want:    []string{"Ctrl+B sidebar", "/ commands"},
			notWant: []string{"Ctrl+Y copy", "Esc interrupt"},
		},
		{
			name:    "medium terminal",
			width:   75,
			want:    []string{"Ctrl+B sidebar", "Ctrl+Y copy", "/ commands"},
			notWant: []string{"Esc interrupt"},
		},
		{
			name: "wide terminal",
			width: 120,
			want: []string{"Ctrl+B sidebar", "Ctrl+Y copy", "Esc interrupt", "/ commands"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := normalFooterHint(tt.width)
			for _, want := range tt.want {
				if !strings.Contains(got, want) {
					t.Errorf("normalFooterHint(%d) = %q; missing %q", tt.width, got, want)
				}
			}
			for _, notWant := range tt.notWant {
				if strings.Contains(got, notWant) {
					t.Errorf("normalFooterHint(%d) = %q; should not contain %q", tt.width, got, notWant)
				}
			}
		})
	}
}
