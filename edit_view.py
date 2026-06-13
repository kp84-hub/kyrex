import re

with open('tui/view.go', 'r') as f:
    content = f.read()

# Replace the sidebar rendering section with cached version
old_sidebar = '''	// --- Sidebar ---
	var sb string
	if showSidebar {
		logo := logoStyle.Render("KYREX")

		// --- ACTIVE FILES Section ---
		activeHeader := sidebarHeaderStyle.Render("ACTIVE FILES")
		var activeContent string
		if len(m.ActiveFiles) == 0 {
			activeContent = lipgloss.NewStyle().Foreground(subtle).Render("None")
		} else {
			var styledActive []string
			for _, f := range m.ActiveFiles {
				styledActive = append(styledActive, lipgloss.NewStyle().Foreground(fg).Render("- "+pathBasename(f)))
			}
			activeContent = strings.Join(styledActive, "\\n")
		}

		// --- WORKSPACE Section ---
		workspaceHeader := sidebarHeaderStyle.Render("WORKSPACE")
		contextStr := lipgloss.NewStyle().Foreground(purple).Render("> " + m.Context)

		var workspaceLines []string
		// Directories
		for _, d := range m.WorkspaceDirs {
			workspaceLines = append(workspaceLines, lipgloss.NewStyle().Foreground(fg).Render("📁 "+d+"/"))
		}
		// Key files
		for _, f := range m.WorkspaceFiles {
			workspaceLines = append(workspaceLines, lipgloss.NewStyle().Foreground(fg).Render("📄 "+f))
		}
		workspaceBody := strings.Join(workspaceLines, "\\n")
		if workspaceBody == "" {
			workspaceBody = lipgloss.NewStyle().Foreground(subtle).Render("No workspace files")
		}

		// --- SESSION Section ---
		sessionHeader := sidebarHeaderStyle.Render("SESSION")
		sessionLines := []string{}
		mode := m.Mode
		if mode == "" {
			mode = string(m.Phase)
		}
		sessionLines = append(sessionLines, lipgloss.NewStyle().Foreground(subtle).Render("mode:   "+mode))
		if m.SessionBranch != "" {
			sessionLines = append(sessionLines, lipgloss.NewStyle().Foreground(subtle).Render("branch: "+m.SessionBranch))
		}
		sessionContent := strings.Join(sessionLines, "\\n")

		// --- EXECUTION TIMELINE Section (only show when there are events) ---
		var timelineSection string
		if len(m.Timeline.Events) > 0 {
			timelineHeader := sidebarHeaderStyle.Render("EXECUTION TIMELINE")
			timelineContent := m.Timeline.Render(sidebarWidth)
			if timelineContent != "" {
				timelineSection = timelineHeader + "\\n" + timelineContent
			}
		}

		var sidebarContent string
		if timelineSection != "" {
			sidebarContent = fmt.Sprintf("%s\\n\\n%s\\n%s\\n\\n%s\\n%s\\n\\n%s\\n\\n%s\\n\\n%s\\n\\n%s",
				logo, activeHeader, activeContent, workspaceHeader, contextStr, workspaceBody, sessionHeader, sessionContent, timelineSection)
		} else {
			sidebarContent = fmt.Sprintf("%s\\n\\n%s\\n%s\\n\\n%s\\n%s\\n\\n%s\\n\\n%s\\n\\n%s",
				logo, activeHeader, activeContent, workspaceHeader, contextStr, workspaceBody, sessionHeader, sessionContent)
		}
		sb = sidebarStyle.Copy().Width(sidebarWidth).Height(m.Height - footerHeight).Render(sidebarContent)
	}
'''

new_sidebar = '''	// --- Sidebar (cached: doesn't change while typing) ---
	var sb string
	if showSidebar {
		mode := m.Mode
		if mode == "" {
			mode = string(m.Phase)
		}
		sidebarKey := fmt.Sprintf("%v|%d|%d|%d|%v|%v|%v|%s|%s|%s|%d",
			showSidebar, sidebarWidth, m.Height, footerHeight,
			m.ActiveFiles, m.WorkspaceDirs, m.WorkspaceFiles,
			m.Context, m.SessionBranch, mode, len(m.Timeline.Events))

		if sidebarKey != m._cachedSidebarKey {
			logo := logoStyle.Render("KYREX")

			// --- ACTIVE FILES Section ---
			activeHeader := sidebarHeaderStyle.Render("ACTIVE FILES")
			var activeContent string
			if len(m.ActiveFiles) == 0 {
				activeContent = lipgloss.NewStyle().Foreground(subtle).Render("None")
			} else {
				var styledActive []string
				for _, f := range m.ActiveFiles {
					styledActive = append(styledActive, lipgloss.NewStyle().Foreground(fg).Render("- "+pathBasename(f)))
				}
				activeContent = strings.Join(styledActive, "\\n")
			}

			// --- WORKSPACE Section ---
			workspaceHeader := sidebarHeaderStyle.Render("WORKSPACE")
			contextStr := lipgloss.NewStyle().Foreground(purple).Render("> " + m.Context)

			var workspaceLines []string
			// Directories
			for _, d := range m.WorkspaceDirs {
				workspaceLines = append(workspaceLines, lipgloss.NewStyle().Foreground(fg).Render("📁 "+d+"/"))
			}
			// Key files
			for _, f := range m.WorkspaceFiles {
				workspaceLines = append(workspaceLines, lipgloss.NewStyle().Foreground(fg).Render("📄 "+f))
			}
			workspaceBody := strings.Join(workspaceLines, "\\n")
			if workspaceBody == "" {
				workspaceBody = lipgloss.NewStyle().Foreground(subtle).Render("No workspace files")
			}

			// --- SESSION Section ---
			sessionHeader := sidebarHeaderStyle.Render("SESSION")
			sessionLines := []string{}
			sessionLines = append(sessionLines, lipgloss.NewStyle().Foreground(subtle).Render("mode:   "+mode))
			if m.SessionBranch != "" {
				sessionLines = append(sessionLines, lipgloss.NewStyle().Foreground(subtle).Render("branch: "+m.SessionBranch))
			}
			sessionContent := strings.Join(sessionLines, "\\n")

			// --- EXECUTION TIMELINE Section (only show when there are events) ---
			var timelineSection string
			if len(m.Timeline.Events) > 0 {
				timelineHeader := sidebarHeaderStyle.Render("EXECUTION TIMELINE")
				timelineContent := m.Timeline.Render(sidebarWidth)
				if timelineContent != "" {
					timelineSection = timelineHeader + "\\n" + timelineContent
				}
			}

			var sidebarContent string
			if timelineSection != "" {
				sidebarContent = fmt.Sprintf("%s\\n\\n%s\\n%s\\n\\n%s\\n%s\\n\\n%s\\n\\n%s\\n\\n%s\\n\\n%s",
					logo, activeHeader, activeContent, workspaceHeader, contextStr, workspaceBody, sessionHeader, sessionContent, timelineSection)
			} else {
				sidebarContent = fmt.Sprintf("%s\\n\\n%s\\n%s\\n\\n%s\\n%s\\n\\n%s\\n\\n%s\\n\\n%s",
					logo, activeHeader, activeContent, workspaceHeader, contextStr, workspaceBody, sessionHeader, sessionContent)
			}
			m._cachedSidebar = sidebarStyle.Copy().Width(sidebarWidth).Height(m.Height - footerHeight).Render(sidebarContent)
			m._cachedSidebarKey = sidebarKey
		}
		sb = m._cachedSidebar
	}
'''

if old_sidebar in content:
    content = content.replace(old_sidebar, new_sidebar)
    print("Replaced sidebar section")
else:
    print("ERROR: Could not find sidebar section")
    # Print a snippet to debug
    idx = content.find("// --- Sidebar ---")
    if idx != -1:
        print("Found '// --- Sidebar ---' at index", idx)
        print(content[idx:idx+500])
    exit(1)

# Replace the footer rendering section with cached version
old_footer = '''	// --- Footer ---
	var toast string
	if m.Toast != "" {
		toast = toastStyle.Render(m.Toast)
	}
	phase := ""
	if m.Phase != PhaseIdle {
		phase = phaseStyle.Render("⚡ " + string(m.Phase))
	}
	brand := brandStyle.Render("KYREX")

	thinking := ""
	if m.IsThinking {
		dots := strings.Repeat(".", (m.Timer%3)+1)
		thinking = timerStyle.Render(fmt.Sprintf("(%ds) Thinking%s", m.Timer, dots))
	}

	modelInfo := lipgloss.NewStyle().Foreground(accent).Render("☁  " + m.LLMInfo)
	dims := lipgloss.NewStyle().Foreground(subtle).Render(fmt.Sprintf(" [%dx%d]", m.Width, m.Height))
	footerContent := lipgloss.JoinHorizontal(lipgloss.Left, phase, brand, "  ", modelInfo, dims, " ", thinking)
	footer := footerStyle.Width(m.Width).Render(footerContent)
'''

new_footer = '''	// --- Footer (cached: doesn't change while typing) ---
	var toast string
	if m.Toast != "" {
		toast = toastStyle.Render(m.Toast)
	}

	footerKey := fmt.Sprintf("%s|%s|%d|%d|%v|%d|%s", m.Phase, m.LLMInfo, m.Width, m.Height, m.IsThinking, m.Timer, m.Toast)
	var footer string
	if footerKey != m._cachedFooterKey {
		phase := ""
		if m.Phase != PhaseIdle {
			phase = phaseStyle.Render("⚡ " + string(m.Phase))
		}
		brand := brandStyle.Render("KYREX")

		thinking := ""
		if m.IsThinking {
			dots := strings.Repeat(".", (m.Timer%3)+1)
			thinking = timerStyle.Render(fmt.Sprintf("(%ds) Thinking%s", m.Timer, dots))
		}

		modelInfo := lipgloss.NewStyle().Foreground(accent).Render("☁  " + m.LLMInfo)
		dims := lipgloss.NewStyle().Foreground(subtle).Render(fmt.Sprintf(" [%dx%d]", m.Width, m.Height))
		footerContent := lipgloss.JoinHorizontal(lipgloss.Left, phase, brand, "  ", modelInfo, dims, " ", thinking)
		m._cachedFooter = footerStyle.Width(m.Width).Render(footerContent)
		m._cachedFooterKey = footerKey
	}
	footer = m._cachedFooter
'''

if old_footer in content:
    content = content.replace(old_footer, new_footer)
    print("Replaced footer section")
else:
    print("ERROR: Could not find footer section")
    exit(1)

with open('tui/view.go', 'w') as f:
    f.write(content)

print("Successfully updated tui/view.go")
