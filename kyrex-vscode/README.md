# Kyrex for VS Code

**Your code never leaves your machine.**

Kyrex is a local-first AI coding agent that runs entirely on your hardware. No telemetry, no cloud relay, no surprise uploads. It connects to any OpenAI-compatible API — or Anthropic — through a Python engine spawned as a child process in your workspace. Everything stays local.

## Key Features

- **Active file context** — Every message automatically includes the content of your active editor tab. Kyrex always knows what you're looking at.
- **Streaming responses** — Token-by-token streaming in the sidebar. Watch reasoning unfold in real time.
- **Tool visibility** — Every tool call is displayed inline with expandable results. See exactly what Kyrex is doing and why.
- **Diff gate** — Full file rewrites open a native VS Code diff view for review and approval. Surgical edits apply directly with a visible inline diff.
- **Session management** — Token tracking, model switching, and one-click session resets. Start fresh or keep context — your call.

## Requirements

- **VS Code** 1.85 or later
- **Python** 3.8+ (the Kyrex engine runs as a local Python subprocess)
- **API key** for any OpenAI-compatible provider (OpenAI, OpenRouter, Ollama, LM Studio, vLLM, etc.) or Anthropic

## Installation

1. Search for **Kyrex** in the Extensions panel.
2. Ensure Python 3.8+ is available on your `PATH`.
3. Set your API key — either via the environment or in settings (see below).
4. Open the Kyrex sidebar and start chatting. The engine starts automatically.

## Configuration

All settings are under `kyrex.*` in your VS Code settings (`Ctrl+,` → search "Kyrex"):

| Setting | Description | Default |
|---|---|---|
| `kyrex.provider` | LLM provider (`openai` or `anthropic`) | `openai` |
| `kyrex.model` | Model name (e.g. `gpt-4o`, `claude-sonnet-4-5-20250929`) | *(empty — uses provider default)* |
| `kyrex.apiKey` | API key *(prefer the `KYREX_API_KEY` env var instead)* | *(empty)* |
| `kyrex.baseUrl` | Custom API endpoint URL | *(empty — uses provider default)* |
| `kyrex.pythonPath` | Path to the Python interpreter | `python3` |

**Recommended:** Set your API key as an environment variable instead of storing it in settings:

```bash
export KYREX_API_KEY="sk-..."
```

## Usage

1. **Open the sidebar** — Click the Kyrex icon in the Activity Bar.
2. **Ask anything** — Type a question and press `Enter`. Your active file is sent as context automatically.
3. **Review tool calls** — Expand any tool call to inspect arguments and results.
4. **Approve edits** — When Kyrex proposes a file change, review the diff and click **Apply** or **Reject**.
5. **Switch models** — Open the Settings panel at the bottom of the sidebar to pick a different model or provider on the fly.
6. **New session** — Click **+ New** to clear context and start fresh.

## Supported Providers

Kyrex works with any **OpenAI-compatible API** out of the box:

| Provider | Base URL | Notes |
|---|---|---|
| **OpenAI** | *(default)* | Set `kyrex.provider` to `openai` |
| **Anthropic** | *(default)* | Set `kyrex.provider` to `anthropic` |
| **OpenRouter** | `https://openrouter.ai/api/v1` | Hundreds of models, single key |
| **Ollama** | `http://localhost:11434/v1` | Fully local, no API key needed |
| **LM Studio** | `http://localhost:1234/v1` | Local models with OpenAI-compatible API |
| **vLLM** | `http://localhost:8000/v1` | High-throughput local inference |
| **Any OpenAI-compatible** | Your endpoint | Set `kyrex.baseUrl` to your server |

To use a custom provider, set `kyrex.baseUrl` to the API endpoint and `kyrex.apiKey` to your key. The model list is fetched automatically from `/v1/models` when you open the Settings panel.

## Commands

| Command | Description |
|---|---|
| `Kyrex: Start Engine` | Manually start the Python engine |
| `Kyrex: Stop Engine` | Stop the engine process |
| `Kyrex: Send Message` | Programmatically send a message |

## License

MIT
