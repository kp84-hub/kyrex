# Kyrex
![Python Version](https://img.shields.io/badge/python-3.12%2B-blue)
![Go Version](https://img.shields.io/badge/go-1.21%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

> A high-integrity, local-first terminal AI agent engineered for autonomous software engineering and systems administration.

![Kyrex TUI Demo](./docs/assets/demo.gif)

Kyrex is not a wrapper around an LLM chat interface. It is a **control plane** that sits between a language model and your local development environment — enforcing safety, tracking state with absolute fidelity, and making every reasoning step observable.

Instead of being another erratic conversationalist, Kyrex transforms modern AI models into predictable, systems-grade engineering controllers.

---

## Core Pillars

### 1. Hardened Safety (The Gates)

Kyrex operates under the philosophy of *"execute first, explain later"* — but wraps that autonomy in strict safety protocols.

- **AST Gating** — Every Python file write is parsed into an Abstract Syntax Tree before touching disk. Syntactically broken code is rejected at the gate, before it can corrupt your codebase.
- **Diff Gating** — No changes land silently. Every file modification is staged and requires explicit human approval via a unified diff preview.
- **Dangerous Command Blocking** — `rm -rf`, `dd`, `mkfs`, `shutdown`, `reboot`, and pipe-to-bash patterns are hard-blocked. Destructive commands require explicit confirmation.

### 2. Version-Controlled Conversations (Tree Mode)

Modeled after Git's branching workflow, Kyrex treats conversation context as a **non-linear tree** rather than a straight line.

- `/branch` — Fork the current context to explore an experimental approach
- `/checkout` — Swap back to a previous branch if the experiment fails
- `/undo` — Rewind to the last user message

Every path is saved locally as portable JSON in `.px_sessions/`.

### 3. The Flight Recorder (Observability)

Kyrex turns internal AI reasoning into structured, chronological infrastructure documentation.

Using a thread-safe auditing system, Kyrex maps exactly **why** a model executed a specific sequence of tools — keeping persistent reasoning logs anchored to each interaction. Not just what the model did. Why it did it.

### 4. Optimized Environment Control

- **Dual Modes** — Toggle between `Plan` (read-only architectural advisory) and `Execute` (autonomous code writing and tool manipulation)
- **Context Compaction** — At 80% token threshold, Kyrex automatically compresses history while preserving critical system paths and active tool states
- **Circuit Breaker** — After 3 consecutive tool failures, Kyrex aborts to prevent infinite reasoning loops
- **Recursion Guard** — Maximum 10 nested tool-call rounds per interaction

### 5. Modular Extensibility

- **MCP Support** — Built-in hooks for the Model Context Protocol to pull in external tools, infrastructure tracking, or search APIs
- **Skills Loader** — Drop `.md` files into `.px_skills/` to inject custom instructions into the agent's context
- **Plugin Registry** — Extend the tool set without touching core logic

---

## Interface

### Go TUI
A keyboard-driven text interface optimized for mobile and narrow terminals:
- Live token and context usage tracking
- Collapsible, chronological thinking blocks
- Immediate tool-trace logging
- Integrated context bar below input
- Real-time streaming responses

### CLI & REPL
Quick one-shot commands or persistent interactive sessions designed to sit alongside active `tmux` panes or code editors.

---

## Architecture

| Component | File(s) | Purpose |
|-----------|---------|---------|
| Core Engine | `kyrex_engine/kyrex/core.py` | `PlaneExecute` class — main agent loop, tool execution, safety gates |
| Config | `kyrex_engine/kyrex/config.py` | `ConfigManager` — API keys, provider settings, env var overrides |
| Providers | `kyrex_engine/kyrex/providers/` | Abstraction layer for OpenAI/Anthropic API calls |
| Tools | `kyrex_engine/kyrex/core.py` + `kyrex_engine/kyrex/tools/` | File editing, search, command execution, MCP integration |
| Session | `kyrex_engine/kyrex/session/` | `TreeSessionManager` — branchable conversation history |
| Skills | `kyrex_engine/kyrex/skills/` | Load custom instructions from `.md` files |
| Extensions | `kyrex_engine/kyrex/extensions/` | Plugin registry for additional tools |
| Audit | `kyrex_engine/kyrex/audit/` | Reasoning capture and flight recorder logging |
| Go TUI | `kyrex/` | Bubble Tea frontend — IPC bridge to Python engine via stdin/stdout JSON frames |

---

## IPC Architecture

The Go TUI and Python engine communicate via **asynchronous IPC over OS pipes** — no ports, no sockets.

```
Go TUI (Bubble Tea)
      │
      │  stdin ──► {"type": "chat", "content": "...", "model": "..."}
      │
Python Engine (kyrex_engine)
      │
      │  stdout ◄── {"type": "token", "delta": "Here is the"}
      │              {"type": "tool_start", "tool": "edit_file"}
      │              {"type": "thought", "delta": "Reading the file..."}
```

**Why pipes beat local RPC:**
- Zero port collisions — safe to run multiple instances in parallel `tmux` panes
- Atomic lifecycle management — if Go crashes, the OS closes the pipe and Python terminates cleanly. No orphaned background processes.

---

## Built-in Tools

| Tool | Description |
|------|-------------|
| `edit_file` | Surgical search-and-replace with AST gate and diff confirmation |
| `write_file` | Create or overwrite a file with full validation pipeline |
| `run_command` | Execute shell commands with 10s timeout and dangerous-command blocking |
| `search` | Recursive regex search across the codebase (up to 50 matches) |
| `read_local_file` | Read full file content |
| `list_local_files` | Recursively list directory contents |
| `query_memory` | Query `.px_memory/` for established project patterns |
| `query_knowledge` | Query `.px_docs/` for project standards and architecture |

---

## Setup

```bash
# Install the engine
cd kyrex_engine && pip install -e .

# Configure your provider
kx --setup

# Launch the TUI
kx

# Or run a one-shot prompt
kx -p "your question here"
```

### Configuration (`~/.px/config.json`)

```json
{
  "provider": "openai",
  "api_key": "your-key-here",
  "base_url": "https://opencode.ai/zen/go/v1",
  "model": "kimi-k2.6"
}
```

---

## Session Commands

| Command | Description |
|---------|-------------|
| `/branch [name]` | Fork current context to a new branch |
| `/checkout <name>` | Switch to an existing branch |
| `/new` | Clear context, start fresh session |
| `/undo` | Rewind to last user message |
| `/tree` | List all session branches |
| `/bookmark <label>` | Bookmark current position |
| `/export` | Export session to HTML |
| `/mode` | Toggle Plan / Execute mode |
| `/model [name]` | View or switch the active model |
| `/skill [name]` | Load a skill into context |
| `/help` | Show all commands |

---

## Design Philosophy

Kyrex is built on one principle: **the model should be powerful, but the environment should be safe.**

Autonomy is not the goal. Predictable, observable, recoverable autonomy is.


---

## Install

One command (Linux/WSL):

    curl -fsSL https://raw.githubusercontent.com/kp84-hub/kyrex/main/install.sh | bash

Requirements: Go 1.21+, Python 3.11+

Manual:

    git clone https://github.com/kp84-hub/kyrex.git
    cd kyrex
    pip install -e kyrex_engine/ --break-system-packages
    go build -o kx .
    sudo cp kx /usr/local/bin/kx
    kx --setup

## CLI

    kx                             # Launch full TUI
    kx --setup                     # Configure provider/model/API key
    kx -p "explain this repo"      # One-shot answer to terminal
    kx -p "find all TODOs" --json  # Machine-readable JSON output

## VS Code Extension

Install the Kyrex extension from the [Visual Studio Code Marketplace](https://marketplace.visualstudio.com/items?itemName=kyrex.kyrex-vscode) by searching for **Kyrex** in the Extensions view.

### macOS Setup

> **Note:** On Mac, Python 3 must be installed via [Homebrew](https://brew.sh). The extension will not work with the system Python that ships with macOS.

1. **Install the extension from the Marketplace.**  
   Open VS Code, go to the Extensions view (`Cmd+Shift+X`), search for **Kyrex**, and click **Install**.

2. **Install the Kyrex engine into the extension directory.**  
   ```bash
   pip3 install --target ~/.vscode/extensions/kyrex.kyrex-vscode-0.1.18/kyrex_engine openai
   ```

3. **Configure Kyrex settings.**  
   Open VS Code Settings (`Cmd+,`) and set:
   - `kyrex.baseUrl` — your API endpoint (e.g., `https://api.openai.com/v1`)
   - `kyrex.apiKey` — your API key
   - `kyrex.model` — the model to use (e.g., `gpt-4o`)

4. **Reload the VS Code window.**  
   Open the Command Palette (`Cmd+Shift+P`), type **Developer: Reload Window**, and press Enter.

After the reload, the Kyrex sidebar will be ready to use with workspace context.

