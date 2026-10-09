# Kyrex for VS Code

**A local Python engine, with your choice of model provider.**

Kyrex runs its coding tools in a Python subprocess in your workspace. Chat, active-file context and relevant tool results are sent to your configured model provider. Choose a local provider such as Ollama to keep model inference on your machine. Remote providers receive the context needed for their responses.

## Key Features

- **Active file context** — Every message automatically includes the content of your active editor tab. Kyrex always knows what you're looking at.
- **Streaming responses** — Token-by-token streaming in the sidebar. Watch reasoning unfold in real time.
- **Tool visibility** — Every tool call is displayed inline with expandable results. See exactly what Kyrex is doing and why.
- **Diff gate** — Full file rewrites open a native VS Code diff view for review and approval. Surgical edits apply directly with a visible inline diff.
- **Session management** — Token tracking, model switching, and one-click session resets. Start fresh or keep context — your call.

## Requirements

- **VS Code** 1.85 or later
- **Python** 3.10+ (the Kyrex engine runs as a local Python subprocess)
- **API key** for any OpenAI-compatible provider (OpenAI, OpenRouter, Ollama, LM Studio, vLLM, etc.) or Anthropic

## Installation

1. Search for **Kyrex** in the Extensions panel.
2. Ensure Python 3.10+ is available on your `PATH`.
3. Install dependencies into the interpreter selected by `kyrex.pythonPath`: `python -m pip install openai anthropic requests textual`. On Windows, use `python` or the full interpreter path if `python3` is unavailable. In WSL/SSH, install into the interpreter on that host.
4. Open the sidebar Settings, select a provider, enter its API key and paste a model ID. Click **Apply settings** once. Wait for **Engine ready**.

To install a prepared update, use **Extensions → … → Install from VSIX**, select the `.vsix`, then run **Developer: Reload Window**. Check the installed version. Marketplace publication is a separate release step.

## Configuration

All settings are under `kyrex.*` in your VS Code settings (`Ctrl+,` → search "Kyrex"):

| Setting | Description | Default |
|---|---|---|
| `kyrex.provider` | LLM provider (`openai` or `anthropic`) | `openai` |
| `kyrex.model` | Model name (e.g. `gpt-4o`, `claude-sonnet-4-5-20250929`) | *(empty — uses provider default)* |
| `kyrex.apiKey` | Legacy setting; global values migrate to SecretStorage | *(empty)* |
| `kyrex.baseUrl` | Custom API endpoint URL | *(empty — uses provider default)* |
| `kyrex.pythonPath` | Path to the Python interpreter | `python3` |

Sidebar keys are stored in VS Code SecretStorage and saved keys are never returned to the sidebar. Leave the key input blank to keep the saved key. **Clear saved key** takes effect on **Apply settings**; environment keys may still be used. Existing workspace-specific legacy keys remain workspace-specific. You can also supply an environment key when launching VS Code:

```bash
export KYREX_API_KEY="sk-..."
```

## Usage

1. **Open the sidebar** — Click the Kyrex icon in the Activity Bar.
2. **Ask anything** — Type a question and press `Enter`. Your active file is sent as context automatically.
3. **Review tool calls** — Expand any tool call to inspect arguments and results.
4. **Approve edits** — When Kyrex proposes a file change, review the diff and click **Apply** or **Reject**.
5. **Switch models** — Open the Settings panel at the bottom of the sidebar to select a provider or paste a model ID, then click **Apply settings**. Typing does not restart the engine.
6. **New session** — Click **+ New** to clear context and start fresh.

## Supported Providers

Kyrex works with any **OpenAI-compatible API** out of the box:

| Provider | Base URL | Notes |
|---|---|---|
| **OpenAI** | *(default)* | Set `kyrex.provider` to `openai` |
| **Anthropic** | *(default)* | Set `kyrex.provider` to `anthropic` |
| **OpenRouter** | `https://openrouter.ai/api/v1` | Hundreds of models, single key |
| **OpenCode Go** | `https://opencode.ai/zen/go/v1` | Paste your model ID |
| **Ollama** | `http://localhost:11434/v1` | Fully local, no API key needed |
| **LM Studio** | `http://localhost:1234/v1` | Local models with OpenAI-compatible API |
| **vLLM** | `http://localhost:8000/v1` | High-throughput local inference |
| **Any OpenAI-compatible** | Your endpoint | Set `kyrex.baseUrl` to your server |

Set a custom API endpoint and key in the sidebar and apply them together. Opening Settings reads the catalogue from the saved endpoint. If it is unavailable, paste a model ID directly. Unapplied drafts are not sent.

## Commands

| Command | Description |
|---|---|
| `Kyrex: Start Engine` | Manually start the Python engine |
| `Kyrex: Stop Engine` | Stop the engine process |
| `Kyrex: Send Message` | Programmatically send a message |

## Building an update

From `kyrex-vscode`:

```bash
npm ci
npm run lint
npm run build
npm test
npm run package
npm run verify:package
```

Build, watch startup and VSIX packaging generate a fresh `kyrex_engine` directory from the shared source. Do not edit the generated directory. Verification compares every packaged runtime file with its source and excludes tests, caches, dependencies and local sessions. CI builds packages on Linux and Windows. The VSIX contains engine source and assets; install Python dependencies separately.

## Disclaimer

Kyrex modifies files on your system. Always use version control. The authors are not responsible for any data loss or damages resulting from use of this software.

## License

MIT
