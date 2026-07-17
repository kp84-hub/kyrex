#!/usr/bin/env python3
import os

with open('/home/kplane/PX/.rifts/kyrex/ab0d9347/tui/view.go', 'r') as f:
    content = f.read()

# Find the corrupted section
old_start = '// Plain bold wordmark'
old_end = 'content := lipgloss.JoinVertical(lipgloss.Center,'

start_idx = content.find(old_start)
end_idx = content.find(old_end, start_idx)
if start_idx < 0 or end_idx < 0:
    print('ERROR: markers not found')
    exit(1)

# Find closing of JoinVertical
after = content[end_idx:]
join_end = after.find('\n\t)\n')
if join_end < 0:
    print('ERROR: closing not found')
    exit(1)
end_idx2 = end_idx + join_end + 4

new_block = '''// Plain bold wordmark — no ASCII art, no clipping risk at any terminal width.
\t// Letter-spaced capitals for a solid, readable form.
\twordmark := lipgloss.NewStyle().
\t\tForeground(accent).
\t\tBold(true).
\t\tRender("K Y R E X")

\t// Active model name (dim/gray using existing subtle color)
\tmodelName := m.Sidebar.CurrentModel
\tif modelName == "" || modelName == "unknown" {
\t\tmodelName = strings.TrimPrefix(m.LLMInfo, "Model: ")
\t\tif modelName == "" {
\t\t\tmodelName = "unknown"
\t\t}
\t}
\tmodelLine := lipgloss.NewStyle().Foreground(subtle).Render("Model: " + modelName)

\t// Session name
\tsession := m.SessionBranch
\tif session == "" {
\t\tsession = "default"
\t}
\tsessionLine := lipgloss.NewStyle().Foreground(subtle).Render("Session: " + session)

\t// Prompt instruction (accent color)
\tpromptLine := lipgloss.NewStyle().Foreground(accent).Bold(true).Render("Type a prompt to begin")

\t// Compact content block — no status pill, no orphaned blank lines
\tcontent := lipgloss.JoinVertical(lipgloss.Center,
\t\twordmark,
\t\t"",
\t\tmodelLine,
\t\tsessionLine,
\t\t"",
\t\tpromptLine,
\t)'''

result = content[:start_idx] + new_block + content[end_idx2:]
with open('/home/kplane/PX/.rifts/kyrex/ab0d9347/tui/view.go', 'w') as f:
    f.write(result)
print('OK')
