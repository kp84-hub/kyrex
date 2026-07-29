# Kyrex
![Python Version](https://img.shields.io/badge/python-3.11%2B-blue)
![Go Version](https://img.shields.io/badge/go-1.24%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Platforms:** Linux & WSL2 · macOS port in progress

> A high-integrity, local-first terminal AI agent engineered for autonomous software engineering and systems administration.

Kyrex is not a wrapper around an LLM chat interface. It is a **control plane** that sits between a language model and your local development environment — enforcing safety, tracking state with absolute fidelity, and making every reasoning step observable.

Instead of being another erratic conversationalist, Kyrex transforms modern AI models into predictable, systems-grade engineering controllers.

---

## Architecture

Kyrex is a **three-layer system**:

| Layer | Language | Location | Purpose |
|-------|----------|----------|---------|
| **TUI** | Go | `main.go` + `tui/` | Bubble Tea terminal interface — keyboard-driven, real-time streaming, sidebar, diff views, overlays |
| **Engine** | Python | `kyrex_engine/kyrex/` | Agent loop, tool execution, session management, safety gates, provider abstraction |
| **IDE** | Rust + TypeScript | `kyrex-ide/` | Tauri desktop app with Monaco editor — sidecar spawns the Python engine, IPC via JSON |

**IPC:** The Go TUI and Python engine communicate over **OS pipes** (stdin/stdout JSON frames) — no ports, no sockets, no orphaned processes.

```
TUI (bubbletea) ←──→ Python Engine (stdin/stdout NDJSON)
                         │
                    Provider API (OpenAI, Anthropic, OpenCode, Ollama...)
```

---

## Core Pillars

### 1. Hardened Safety (The Gates)

Kyrex operates under the philosophy of *"execute first, explain later"* — but wraps that autonomy in strict safety protocols.

- **AST Gating** — Every Python file write is parsed into an Abstract Syntax Tree before touching disk. Syntactically broken code is rejected at the gate.
- **Diff Gating** — Every file modification is staged and requires explicit human approval via a unified diff preview. Auto-approve with configurable delay supported.
- **Dangerous Command Blocking** — `rm -rf`, `dd`, `mkfs`, `shutdown`, `reboot`, and pipe-to-bash patterns are hard-blocked. Destructive commands (`sudo`, piped to `sh`) require explicit confirmation. File deletions go through a dedicated approval gate.
- **300s Tool Timeout** — Individual tool calls are bounded by `KYREX_TOOL_TIMEOUT` (default 300s) with a threading.Timer backup.
- **Circuit Breaker** — After 3 consecutive tool failures, Kyrex aborts the reasoning loop to prevent infinite error loops.
- **Recursion Guard** — Maximum 25 nested tool-call rounds per turn (`KYREX_MAX_RECURSION`). After that, the loop terminates.
- **Loop Detection** — 3 consecutive identical tool-call fingerprints triggers early abort.

### 2. Version-Controlled Conversations (Tree Mode)

Modeled after Git's branching workflow, Kyrex treats conversation context as a **non-linear tree** rather than a straight line.

- `/branch [name]` — Fork the current context to explore an experimental approach (or auto-generate a name)
- `/checkout <name>` — Swap back to a previous branch
- `/undo` — Rewind to the last user message
- `/tree` — List all session branches with current branch highlighted
- `/bookmark <label>` — Bookmark a position in history
- `/export` — Export the full conversation to a standalone HTML file

Every path is saved locally as portable JSON in `.px_sessions/`.

**Session persistence:** Branch state persists across app restarts. The "new session" button in the IDE frontend uses `/branch` to fork from the current state rather than `/new`, ensuring the new session is saved under its own branch file.

### 3. The Flight Recorder (Observability)

Kyrex turns internal AI reasoning into structured, chronological infrastructure documentation.

Using a thread-safe auditing system (`ReasoningAuditLogger`), Kyrex maps exactly **why** a model executed a specific sequence of tools — keeping persistent reasoning logs anchored to each interaction. The audit log records:

- Reasoning blocks with timestamps and working directories
- Every tool call with its arguments
- Files read and written during each block
- Block sequence numbers

Output is written to `.px_history` as markdown-formatted chronological entries.

### 4. Race Mode & Consult Mode

**Race Mode** (`/race`) — Run the same coding task across multiple models in parallel, each in an isolated clone of the workspace. Results are compared side-by-side, with per-lane diffs and gate evaluations.

```
/race "Add error handling to the auth module" --models gpt-4o,claude-3-5-sonnet
```

Features:
- Isolated workspace clones per lane via the `rift` package (copy-on-write)
- Per-lane model configuration
- Side-by-side diff comparison after completion
- Gate evaluation (automated correctness checks)
- Merge selection

**Consult Mode** (`/consult`) — Lightweight 2-model parallel consultation with side-by-side comparison. Same isolation model as race mode, designed for quick opinion gathering rather than full parallel execution.

### 5. Optimized Environment Control

- **Dual Modes** — Toggle between `Plan` (read-only architectural advisory) and `Execute` (autonomous code writing and tool manipulation)
- **Context Compaction** — At 80% token threshold, Kyrex automatically compresses history while preserving critical system paths and active tool states. Preserves the last 15 messages untouched.
- **Progress Tracking** — Multi-step requests are tracked with a numbered checklist that the agent checks off as it completes each step.
- **Interrupt Support** — Press Esc to cancel a running turn mid-execution. The engine receives a threading.Event signal and exits cleanly.

### 6. Modular Extensibility

- **MCP Support** — Built-in hooks for the Model Context Protocol (`/mcp add/remove`) to pull in external tools, infrastructure tracking, or search APIs. Servers are started on engine boot via `MCPManager`.
- **Skills Loader** — Drop `.md` files into `.px_skills/` or `~/.kyrex/skills/` to inject custom instructions into the agent's context. Skills are auto-matched by keyword on user input and can be loaded explicitly with `/skill [name]`.
- **Plugin Registry** — Extend the tool set via Python decorators in `~/.kyrex/extensions/` or `.px_extensions/`. Registered tools automatically appear in the model's tool schema.

---

## Interface

### Go TUI

A keyboard-driven text interface optimized for terminal use:

- Live token and context usage tracking in the sidebar
- Collapsible, chronological thinking blocks (reasoning overlay)
- Real-time streaming responses with token coalescing (16ms batch window)
- Tool telemetry ring buffer (last 50 tool calls with state/duration)
- Execution timeline component with phase transitions
- Integrated diff viewer (side-by-side for edits, single-box for deletions)
- Auto-approve with 5-second configurable delay on diff gates
- Model picker overlay (fetches available models from the provider API)
- Usage stats overlay (`/usage`)
- Command palette (Ctrl+P / Cmd+P)
- Paste burst detection (large pastes show "[Pasted ~N lines]" placeholder)
- Mouse support (toggleable)

### Kyrex IDE (Tauri + React)

A full desktop application built with Tauri, React, TypeScript, and Monaco Editor:

- Sidecar spawns the Python engine as a child process
- JSON-based IPC via engine stdout
- Session management sidebar (list all branches, switch, create new)
- File browser with workspace tree
- Built-in Monaco code editor with diff view
- File attachment support
- Split pane layout (chat + editor)

### CLI

```
kx                              Launch full TUI
kx --setup                      Interactive provider/model/API key configuration wizard
kx -p "explain this repo"       One-shot answer to terminal
kx -p "find all TODOs" --json   Machine-readable JSON output
kx --update                     Git pull + rebuild from ~/kyrex
```

---

## Setup

### One-command install (Linux/WSL)

```bash
curl -fsSL https://raw.githubusercontent.com/kp84-hub/kyrex/main/install.sh | bash
kx --setup
```

### Manual install

```bash
git clone https://github.com/kp84-hub/kyrex.git
cd kyrex
pip install -e kyrex_engine/
go build -o kx .
sudo cp kx /usr/local/bin/kx
kx --setup
```

### Configuration Wizard

`kx --setup` walks through:
1. **Provider** — OpenCode (recommended), OpenRouter, OpenAI, Anthropic, Ollama (local), or custom
2. **API Base URL** — auto-filled for presets, manual for custom
3. **Authentication** — enter an API key directly or specify an environment variable name (e.g. `$MY_API_KEY`)
4. **Model** — fetches available models from the provider API, or manual entry
5. **Custom Headers** — optional key=value pairs (e.g. for provider-specific auth)
6. **Connection Test** — validates the configuration with a live API call
7. **Save** — writes to `~/.px/config.json`

### Configuration file (`~/.px/config.json`)

```json
{
  "provider": "openai",
  "api_key": "your-key-here",
  "api_key_env": "MY_API_KEY",
  "base_url": "https://opencode.ai/zen/go/v1",
  "model": "kimi-k2.6",
  "headers": {
    "x-custom-header": "value"
  }
}
```

Environment variables take priority over config values: `KYREX_API_KEY`, `KYREX_PROVIDER`, `KYREX_MODEL`, `KYREX_BASE_URL`, `KYREX_CONTEXT_LIMIT`, `KYREX_MAX_RECURSION`, `KYREX_TOOL_TIMEOUT`, `KYREX_MCP_TIMEOUT`, `KYREX_SURFACE`.

---

## Engine Architecture

### Components

| Module | Location | Purpose |
|--------|----------|---------|
| **Core Loop** | `kyrex/core.py` | `PlaneExecute` — agent orchestration, tool dispatch, safety gates, interrupt handling |
| **Session Tree** | `kyrex/session/tree.py` | `TreeSessionManager` — branchable conversation history with JSON persistence |
| **Config** | `kyrex/config.py` | `ConfigManager` — provider settings, API keys, env var resolution, setup wizard |
| **Providers** | `kyrex/providers/` | Abstraction layer: `BaseProvider` → `OpenAIProvider`, `AnthropicProvider` |
| **Tools** | `kyrex/toolbox.py` | Built-in tool implementations (edit, read, search, run) |
| **MCP** | `kyrex/tools/mcp.py` | Model Context Protocol server management with stdio transport |
| **Extensions** | `kyrex/extensions/registry.py` | Plugin registry with auto-discovery from `~/.kyrex/extensions/` |
| **Skills** | `kyrex/skills/loader.py` | Markdown skill loader with keyword-based auto-matching |
| **Audit** | `kyrex/audit/reasoning.py` | Thread-safe reasoning capture and `.px_history` logging |
| **Modes** | `kyrex/modes/` | `interactive` (REPL), `rpc` (stdin JSON protocol), `print_` (one-shot) |
| **Bridge** | `core_bridge.py` | stdin/stdout IPC daemon — relays JSON frames between engine and TUI |

### Provider Support

- **OpenAI-compatible** — OpenAI, OpenRouter, OpenCode, Ollama (local), any OpenAI-API-compatible endpoint
- **Anthropic** — Native Anthropic API with extended thinking support
- **Automatic retry** — Exponential backoff with jitter on rate limits and transient errors. Auth errors (401) never retry.
- **Streaming** — Token-by-token streaming to TUI with progressive final-round detection
- **Model pricing** — Cost estimation for common models (gpt-4o, claude-3-5-sonnet, etc.)

### Built-in Tools

| Tool | Description |
|------|-------------|
| `edit_file` | Surgical search-and-replace with AST gate and diff confirmation |
| `write_file_with_gate` | Create or overwrite a file with full validation pipeline and human diff gate |
| `run_command` | Execute shell commands (default 300s timeout) with dangerous-command blocking |
| `search` | Recursive regex search across the codebase (up to 50 matches) |
| `read_local_file` | Read file content with offset/limit support |
| `list_local_files` | Recursively list directory contents |
| `query_memory` | Query `.px_memory/` for established project patterns |
| `query_knowledge` | Query `.px_docs/` for project standards and architecture |
| `task_complete` | Signal task completion with summary (tracks progress via numbered checklist) |

---

## Session Commands

| Command | Description |
|---------|-------------|
| `/branch [name]` | Fork current context to a new branch (auto-generates name if omitted) |
| `/checkout <name>` | Switch to an existing session branch |
| `/clear` | Clear context, start fresh session with new branch |
| `/undo` | Rewind to the last user message |
| `/tree` | List all session branches with current branch marked |
| `/bookmark <label>` | Bookmark current position in history |
| `/export` | Export session to standalone HTML |
| `/model [name]` | View or switch the active LLM model (fetches available models from provider) |
| `/skill [name]` | List available skills or load one into context |
| `/mcp [add/remove]` | Manage MCP servers (add/remove/list) |
| `/spawn <prompt>` | Fork a background subprocess with the given prompt |
| `/usage` | Display token usage statistics and cost estimate (overlay) |
| `/help` | Show all available commands |

---

## Session Persistence

Conversations are stored as JSON files in `.px_sessions/`. Each branch maps to a file named `{branch_name}.json` containing:

```json
{
  "branch_name": "main",
  "fork_index": 0,
  "history": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool", "content": "...", "tool_call_id": "..."}
  ],
  "labels": {"15": "before-refactor"}
}
```

The `main` branch is loaded on startup. Any branch can be checked out by name. The in-memory `_branch_fork` map tracks fork points, and checkout also checks disk for orphaned branch files.

---

## Design Philosophy

Kyrex is built on one principle: **the model should be powerful, but the environment should be safe.**

Autonomy is not the goal. Predictable, observable, recoverable autonomy is.

Every safety mechanism (AST gates, diff previews, circuit breakers, loop detection, interrupt support, timeout boundaries) exists to make the agent's behavior **bounded** — so the user can trust it to act autonomously within clearly defined limits, and intervene the moment those limits are exceeded.

---

## Quick Start

```bash
# Configure
kx --setup

# Launch TUI
kx

# New session (IDE frontend)
# Click "+ New Session" or send "/branch"

# Race Mode (run task across models in parallel)
/race "Add unit tests" --models gpt-4o,claude-3-5-sonnet

# Consult Mode (lightweight parallel opinion)
/consult "Review this error handling" --models gpt-4o,claude-3-5-sonnet
```