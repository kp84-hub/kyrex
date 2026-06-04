"use strict";
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// src/extension.ts
var extension_exports = {};
__export(extension_exports, {
  activate: () => activate,
  deactivate: () => deactivate
});
module.exports = __toCommonJS(extension_exports);
var vscode = __toESM(require("vscode"));
var import_child_process = require("child_process");
var os = __toESM(require("os"));
var path = __toESM(require("path"));
var fs = __toESM(require("fs"));
var engineProcess = null;
function activate(context) {
  const outputChannel = vscode.window.createOutputChannel("Kyrex Engine");
  outputChannel.appendLine("Kyrex VS Code extension activated.");
  const sidebarProvider = new KyrexSidebarProvider(context.extensionUri, outputChannel);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("kyrex-vscode.sidebar", sidebarProvider)
  );
  const startCmd = vscode.commands.registerCommand("kyrex-vscode.start", () => {
    startEngine(context, outputChannel, sidebarProvider);
  });
  context.subscriptions.push(startCmd);
  const stopCmd = vscode.commands.registerCommand("kyrex-vscode.stop", () => {
    stopEngine(outputChannel);
  });
  context.subscriptions.push(stopCmd);
  const sendCmd = vscode.commands.registerCommand("kyrex-vscode.sendMessage", (text) => {
    sendToEngine(text, outputChannel);
  });
  context.subscriptions.push(sendCmd);
  startEngine(context, outputChannel, sidebarProvider);
}
function deactivate() {
  stopEngine(void 0);
}
function startEngine(context, output, sidebarProvider) {
  if (engineProcess) {
    output.appendLine("Engine already running.");
    return;
  }
  const config = vscode.workspace.getConfiguration("kyrex");
  const pythonPath = config.get("pythonPath", "python3");
  const bridgeScript = "/home/kplane/PX/kyrex/kyrex_engine/core_bridge.py";
  output.appendLine(`Starting engine: ${pythonPath} ${bridgeScript}`);
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  engineProcess = (0, import_child_process.spawn)(pythonPath, [bridgeScript], {
    cwd: path.dirname(workspaceRoot),
    env: {
      ...process.env,
      KYREX_VSCODE: "1",
      KYREX_PROVIDER: config.get("provider", "openai"),
      KYREX_MODEL: config.get("model", ""),
      KYREX_API_KEY: config.get("apiKey", process.env.KYREX_API_KEY || ""),
      KYREX_BASE_URL: config.get("baseUrl", ""),
      // Mirror keys to standard variables so the Python backend connects cleanly to OpenCode
      OPENAI_API_KEY: config.get("apiKey", process.env.KYREX_API_KEY || ""),
      OPENAI_BASE_URL: config.get("baseUrl", "") || void 0
    },
    stdio: ["pipe", "pipe", "pipe"]
  });
  engineProcess.stdout?.on("data", (data) => {
    const lines = data.toString().split("\n").filter((l) => l.trim());
    for (const line of lines) {
      try {
        const msg = JSON.parse(line);
        if (msg.type === "vscode_action") {
          if (msg.action === "get_active_file") {
            const editor = vscode.window.activeTextEditor;
            const replyPayload = {
              type: "action_result",
              action: "get_active_file",
              filePath: editor ? editor.document.fileName : null,
              content: editor ? editor.document.getText() : null
            };
            if (engineProcess && engineProcess.stdin) {
              engineProcess.stdin.write(JSON.stringify(replyPayload) + "\n");
            }
          }
          continue;
        }
        if (msg.type === "propose_edit") {
          handleProposeEdit(msg, output);
          continue;
        }
        sidebarProvider.postMessage({ type: "engine", payload: msg });
      } catch {
        output.appendLine(`[engine stdout] ${line}`);
      }
    }
  });
  engineProcess.stderr?.on("data", (data) => {
    output.appendLine(`[engine stderr] ${data.toString().trim()}`);
  });
  engineProcess.on("close", (code) => {
    output.appendLine(`Engine exited with code ${code}`);
    engineProcess = null;
    sidebarProvider.postMessage({ type: "engine_status", payload: { running: false } });
  });
  engineProcess.on("error", (err) => {
    output.appendLine(`Engine spawn error: ${err.message}`);
    vscode.window.showErrorMessage(`Kyrex engine failed to start: ${err.message}`);
    engineProcess = null;
  });
  sidebarProvider.postMessage({ type: "engine_status", payload: { running: true } });
  output.appendLine("Engine started.");
}
async function handleProposeEdit(msg, output) {
  const { filePath, content } = msg;
  output.appendLine(`[propose_edit] Incoming edit for: ${filePath}`);
  const tmpDir = os.tmpdir();
  const base = path.basename(filePath);
  const tmpFile = path.join(tmpDir, `.kyrex_propose_${Date.now()}_${base}`);
  fs.writeFileSync(tmpFile, content, "utf-8");
  output.appendLine(`[propose_edit] Temp file: ${tmpFile}`);
  const originalUri = vscode.Uri.file(filePath);
  const modifiedUri = vscode.Uri.file(tmpFile);
  const title = `Kyrex: Proposed change to ${base}`;
  output.appendLine(`[propose_edit] Opening diff...`);
  try {
    await vscode.commands.executeCommand(
      "vscode.diff",
      originalUri,
      modifiedUri,
      title
    );
  } catch (e) {
    output.appendLine(`[propose_edit] diff error: ${e.message}`);
  }
  const result = await vscode.window.showInformationMessage(
    `Apply this change to ${base}?`,
    { modal: true },
    "Apply",
    "Reject"
  );
  output.appendLine(`[propose_edit] User chose: ${result}`);
  if (result === "Apply") {
    try {
      fs.writeFileSync(filePath, content, "utf-8");
      output.appendLine(`[propose_edit] Wrote: ${filePath}`);
      vscode.window.showInformationMessage(`Applied: ${base}`);
    } catch (e) {
      output.appendLine(`[propose_edit] Write error: ${e.message}`);
      vscode.window.showErrorMessage(`Failed to apply: ${e.message}`);
    }
  }
  try {
    fs.unlinkSync(tmpFile);
  } catch {
  }
}
async function stopEngine(output) {
  if (engineProcess) {
    engineProcess.kill();
    engineProcess = null;
    output?.appendLine("Engine stopped.");
  }
}
function sendToEngine(text, output) {
  output.appendLine(`[DEBUG TRACER] sendToEngine called with: ${text.slice(0, 50)}`);
  if (!engineProcess || !engineProcess.stdin) {
    vscode.window.showWarningMessage("Kyrex engine is not running. Start it first.");
    return;
  }
  const payloadObj = { type: "chat", content: text };
  let doc = vscode.window.activeTextEditor?.document;
  if (!doc && vscode.window.visibleTextEditors.length > 0) {
    doc = vscode.window.visibleTextEditors[0].document;
  }
  if (!doc) {
    const openDocs = vscode.workspace.textDocuments.filter((d) => d.uri.scheme === "file" && !d.fileName.includes(".git"));
    if (openDocs.length > 0) {
      doc = openDocs[0];
    }
  }
  if (doc && (doc.uri.scheme !== "file" || doc.fileName.includes("extension-output"))) {
    doc = void 0;
  }
  output.appendLine(`[DEBUG TRACER] doc check: activeEditor=${!!vscode.window.activeTextEditor}, visible=${vscode.window.visibleTextEditors.length}, open=${vscode.workspace.textDocuments.filter((d) => d.uri.scheme === "file").length}`);
  if (doc) {
    payloadObj.activeFile = {
      path: doc.fileName,
      content: doc.getText()
    };
    output.appendLine(`[DEBUG TRACER] Attached file context: ${doc.fileName}`);
  } else {
    output.appendLine(`[DEBUG TRACER] WARNING: No active, visible, or open workspace file found!`);
  }
  const payload = JSON.stringify(payloadObj) + "\n";
  engineProcess.stdin.write(payload);
  output.appendLine(`Sent: ${text.slice(0, 80)}`);
}
var KyrexSidebarProvider = class {
  constructor(extensionUri, output) {
    this.extensionUri = extensionUri;
    this.output = output;
  }
  _view;
  resolveWebviewView(webviewView) {
    this._view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri]
    };
    webviewView.webview.html = this.getHtml();
    webviewView.webview.onDidReceiveMessage((msg) => {
      switch (msg.type) {
        case "send":
          vscode.commands.executeCommand("kyrex-vscode.sendMessage", msg.text);
          break;
        case "interrupt":
          if (engineProcess?.stdin) {
            engineProcess.stdin.write(
              JSON.stringify({ type: "interrupt" }) + "\n"
            );
          }
          break;
        case "clear_ui":
          this.postMessage({ type: "clear_ui" });
          break;
      }
    });
  }
  postMessage(msg) {
    this._view?.webview.postMessage(msg);
  }
  getHtml() {
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
    <button id="new-session-btn" title="New Session">+ New</button>
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
    document.getElementById("new-session-btn").addEventListener("click", () => {
      vscode.postMessage({ type: "send", text: "/clear" });
      vscode.postMessage({ type: "clear_ui" });
    });
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
              addMessage("tool", "\u{1F527} " + (p.name || "tool"));
              break;
            case "tool_result":
              break;
            case "error":
              addMessage("error", "\u26A0 " + (p.content || "Error"));
              break;
            case "session_state":
              statusEl.textContent = "Engine: ready \u2022 " + (p.model || "unknown");
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
        case "clear_ui": {
          messagesEl.innerHTML = '';
          break;
        }
      }
    });
  </script>
</body>
</html>`;
  }
};
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  activate,
  deactivate
});
//# sourceMappingURL=extension.js.map
