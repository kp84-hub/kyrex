import * as vscode from "vscode";
import { ChildProcess, spawn } from "child_process";

let engineProcess: ChildProcess | null = null;

export function activate(context: vscode.ExtensionContext) {
  const outputChannel = vscode.window.createOutputChannel("Kyrex Engine");
  outputChannel.appendLine("Kyrex VS Code extension activated.");

  // ── Register sidebar webview provider ──────────────────────────
  const sidebarProvider = new KyrexSidebarProvider(context.extensionUri, outputChannel);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("kyrex-vscode.sidebar", sidebarProvider)
  );

  // ── Command: Start Engine ──────────────────────────────────────
  const startCmd = vscode.commands.registerCommand("kyrex-vscode.start", () => {
    startEngine(context, outputChannel, sidebarProvider);
  });
  context.subscriptions.push(startCmd);

  // ── Command: Stop Engine ───────────────────────────────────────
  const stopCmd = vscode.commands.registerCommand("kyrex-vscode.stop", () => {
    stopEngine(outputChannel);
  });
  context.subscriptions.push(stopCmd);

  // ── Command: Send Message ──────────────────────────────────────
  const sendCmd = vscode.commands.registerCommand("kyrex-vscode.sendMessage", (text: string) => {
    sendToEngine(text, outputChannel);
  });
  context.subscriptions.push(sendCmd);

  // ── Auto-start engine on activation ────────────────────────────
  startEngine(context, outputChannel, sidebarProvider);
}

export function deactivate() {
  stopEngine(undefined);
}

// ── Engine lifecycle ─────────────────────────────────────────────

function startEngine(
  context: vscode.ExtensionContext,
  output: vscode.OutputChannel,
  sidebarProvider: KyrexSidebarProvider
) {
  if (engineProcess) {
    output.appendLine("Engine already running.");
    return;
  }

  const config = vscode.workspace.getConfiguration("kyrex");
  const pythonPath: string = config.get("pythonPath", "python3");

// Explicitly point to your local development file path
  const bridgeScript = "/home/kplane/PX/kyrex/kyrex_engine/core_bridge.py";

  output.appendLine(`Starting engine: ${pythonPath} ${bridgeScript}`);

  engineProcess = spawn(pythonPath, [bridgeScript], {
    env: {
// ... rest of your code remains exactly the same
      ...process.env,
      KYREX_PROVIDER: config.get("provider", "openai"),
      KYREX_MODEL: config.get("model", ""),
      KYREX_API_KEY: config.get("apiKey", process.env.KYREX_API_KEY || ""),
      KYREX_BASE_URL: config.get("baseUrl", ""),
    },
    stdio: ["pipe", "pipe", "pipe"],
  });

  engineProcess.stdout?.on("data", (data: Buffer) => {
    const lines = data.toString().split("\n").filter((l) => l.trim());
    for (const line of lines) {
      try {
        const msg = JSON.parse(line);
        sidebarProvider.postMessage({ type: "engine", payload: msg });
      } catch {
        output.appendLine(`[engine stdout] ${line}`);
      }
    }
  });

  engineProcess.stderr?.on("data", (data: Buffer) => {
    output.appendLine(`[engine stderr] ${data.toString().trim()}`);
  });

  engineProcess.on("close", (code: number | null) => {
    output.appendLine(`Engine exited with code ${code}`);
    engineProcess = null;
    sidebarProvider.postMessage({ type: "engine_status", payload: { running: false } });
  });

  engineProcess.on("error", (err: Error) => {
    output.appendLine(`Engine spawn error: ${err.message}`);
    vscode.window.showErrorMessage(`Kyrex engine failed to start: ${err.message}`);
    engineProcess = null;
  });

  sidebarProvider.postMessage({ type: "engine_status", payload: { running: true } });
  output.appendLine("Engine started.");
}

function stopEngine(output?: vscode.OutputChannel) {
  if (engineProcess) {
    engineProcess.kill();
    engineProcess = null;
    output?.appendLine("Engine stopped.");
  }
}

function sendToEngine(text: string, output: vscode.OutputChannel) {
  if (!engineProcess || !engineProcess.stdin) {
    vscode.window.showWarningMessage("Kyrex engine is not running. Start it first.");
    return;
  }
  const payload = JSON.stringify({ type: "chat", content: text }) + "\n";
  engineProcess.stdin.write(payload);
  output.appendLine(`Sent: ${text.slice(0, 80)}`);
}

// ── Sidebar Webview Provider ─────────────────────────────────────

class KyrexSidebarProvider implements vscode.WebviewViewProvider {
  private _view?: vscode.WebviewView;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly output: vscode.OutputChannel
  ) {}

  resolveWebviewView(webviewView: vscode.WebviewView) {
    this._view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };
    webviewView.webview.html = this.getHtml();

    // Handle messages from the webview
    webviewView.webview.onDidReceiveMessage((msg) => {
      switch (msg.type) {
        case "send":
          vscode.commands.executeCommand("kyrex-vscode.sendMessage", msg.text);
          break;
        case "interrupt":
          // Send interrupt to engine via stdin
          if (engineProcess?.stdin) {
            engineProcess.stdin.write(
              JSON.stringify({ type: "interrupt" }) + "\n"
            );
          }
          break;
      }
    });
  }

  postMessage(msg: any) {
    this._view?.webview.postMessage(msg);
  }

  private getHtml(): string {
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Kyrex Chat</title>
  <style>
    :root {
      --bg: var(--vscode-sideBar-background);
      --fg: var(--vscode-sideBar-foreground);
      --accent: var(--vscode-textLink-foreground);
      --border: var(--vscode-sideBar-border);
      --input-bg: var(--vscode-input-background);
      --input-fg: var(--vscode-input-foreground);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      background: var(--bg);
      color: var(--fg);
      height: 100vh;
      display: flex;
      flex-direction: column;
    }
    #messages {
      flex: 1;
      overflow-y: auto;
      padding: 8px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .msg {
      padding: 6px 10px;
      border-radius: 6px;
      max-width: 100%;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .msg.user {
      align-self: flex-end;
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
    }
    .msg.assistant {
      align-self: flex-start;
      background: var(--vscode-editor-background);
      border: 1px solid var(--border);
    }
    .msg.tool {
      align-self: flex-start;
      background: transparent;
      color: var(--vscode-descriptionForeground);
      font-style: italic;
      font-size: 0.9em;
    }
    .msg.error {
      align-self: flex-start;
      color: var(--vscode-errorForeground);
      font-weight: bold;
    }
    #input-area {
      display: flex;
      padding: 8px;
      border-top: 1px solid var(--border);
      gap: 6px;
    }
    #input-area textarea {
      flex: 1;
      resize: none;
      min-height: 28px;
      max-height: 120px;
      padding: 4px 8px;
      background: var(--input-bg);
      color: var(--input-fg);
      border: 1px solid var(--border);
      border-radius: 4px;
      font-family: inherit;
      font-size: inherit;
    }
    #input-area button {
      padding: 4px 12px;
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: none;
      border-radius: 4px;
      cursor: pointer;
    }
    #input-area button:hover {
      background: var(--vscode-button-hoverBackground);
    }
    .status {
      padding: 2px 8px;
      font-size: 0.8em;
      color: var(--vscode-descriptionForeground);
    }
  </style>
</head>
<body>
  <div id="messages"></div>
  <div id="input-area">
    <textarea id="prompt" rows="1" placeholder="Ask Kyrex..."></textarea>
    <button id="send-btn">Send</button>
  </div>
  <div class="status" id="status">Engine: starting...</div>

  <script>
    const vscode = acquireVsCodeApi();
    const messagesEl = document.getElementById("messages");
    const promptEl = document.getElementById("prompt");
    const sendBtn = document.getElementById("send-btn");
    const statusEl = document.getElementById("status");
    let currentAssistantMsg = null;

    function addMessage(role, text) {
      const div = document.createElement("div");
      div.className = "msg " + role;
      div.textContent = text;
      messagesEl.appendChild(div);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      return div;
    }

    function appendToken(text) {
      if (!currentAssistantMsg) {
        currentAssistantMsg = addMessage("assistant", "");
      }
      currentAssistantMsg.textContent += text;
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function finalizeAssistant() {
      currentAssistantMsg = null;
    }

    function send() {
      const text = promptEl.value.trim();
      if (!text) return;
      addMessage("user", text);
      promptEl.value = "";
      vscode.postMessage({ type: "send", text });
    }

    sendBtn.addEventListener("click", send);
    promptEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });

    window.addEventListener("message", (e) => {
      const msg = e.data;
      if (!msg) return;

      switch (msg.type) {
        case "engine": {
          const p = msg.payload;
          switch (p.type) {
            case "token":
              appendToken(p.content || "");
              break;
            case "chat_done":
              finalizeAssistant();
              break;
            case "tool_start":
              addMessage("tool", "🔧 " + (p.name || "tool"));
              break;
            case "tool_result":
              // tool results are implicit, could add details here
              break;
            case "error":
              addMessage("error", "⚠ " + (p.content || "Error"));
              break;
            case "session_state":
              statusEl.textContent = "Engine: ready • " + (p.model || "unknown");
              break;
            case "phase":
              if (p.value === "IDLE") {
                finalizeAssistant();
              }
              break;
            default:
              break;
          }
          break;
        }
        case "engine_status": {
          statusEl.textContent = msg.payload.running
            ? "Engine: running"
            : "Engine: stopped";
          break;
        }
      }
    });
  </script>
</body>
</html>`;
  }
}
