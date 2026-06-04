import * as vscode from "vscode";
import { ChildProcess, spawn } from "child_process";
import * as os from "os";
import * as path from "path";
import * as fs from "fs";

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

  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;

  engineProcess = spawn(pythonPath, [bridgeScript], {
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
      OPENAI_BASE_URL: config.get("baseUrl", "") || undefined
    },
    stdio: ["pipe", "pipe", "pipe"],
  });

  engineProcess.stdout?.on("data", (data: Buffer) => {
    const lines = data.toString().split("\n").filter((l: any) => l.trim());
    for (const line of lines) {
      try {
        const msg = JSON.parse(line);

        // ── 1. INTERCEPT VS CODE NATIVE ACTIONS ──
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

        // ── 1.5 INTERCEPT PROPOSE_EDIT FROM ENGINE ──
        if (msg.type === "propose_edit") {
          handleProposeEdit(msg, output);
          continue;
        }

        // ── 2. ROUTE NORMAL CHAT TO SIDEBAR ──
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

async function handleProposeEdit(
  msg: { filePath: string; content: string },
  output: vscode.OutputChannel
) {
  const { filePath, content } = msg;
  output.appendLine(`[propose_edit] Incoming edit for: ${filePath}`);

  // 1. Write proposed content to a temp file
  const tmpDir = os.tmpdir();
  const base = path.basename(filePath);
  const tmpFile = path.join(tmpDir, `.kyrex_propose_${Date.now()}_${base}`);
  fs.writeFileSync(tmpFile, content, "utf-8");
  output.appendLine(`[propose_edit] Temp file: ${tmpFile}`);

  // 2. Open VS Code diff
  const originalUri = vscode.Uri.file(filePath);
  const modifiedUri = vscode.Uri.file(tmpFile);
  const title = `Kyrex: Proposed change to ${base}`;
  output.appendLine(`[propose_edit] Opening diff...`);

  try {
    // showTextDocument with diff view — use the command approach for reliable diff
    await vscode.commands.executeCommand(
      "vscode.diff",
      originalUri,
      modifiedUri,
      title
    );
  } catch (e: any) {
    output.appendLine(`[propose_edit] diff error: ${e.message}`);
  }

  // 3. Ask user to apply or reject
  const result = await vscode.window.showInformationMessage(
    `Apply this change to ${base}?`,
    { modal: true },
    "Apply",
    "Reject"
  );

  output.appendLine(`[propose_edit] User chose: ${result}`);

  // 4. Wire result back to engine stdin so the tool call can resolve
  if (result === "Apply") {
    try {
      fs.writeFileSync(filePath, content, "utf-8");
      output.appendLine(`[propose_edit] Wrote: ${filePath}`);
      vscode.window.showInformationMessage(`Applied: ${base}`);
    } catch (e: any) {
      output.appendLine(`[propose_edit] Write error: ${e.message}`);
      vscode.window.showErrorMessage(`Failed to apply: ${e.message}`);
    }
  }

  // Clean up temp file
  try {
    fs.unlinkSync(tmpFile);
  } catch {}
}

async function stopEngine(output?: vscode.OutputChannel) {
  if (engineProcess) {
    engineProcess.kill();
    engineProcess = null;
    output?.appendLine("Engine stopped.");
  }
}

function sendToEngine(text: string, output: vscode.OutputChannel) {
  output.appendLine(`[DEBUG TRACER] sendToEngine called with: ${text.slice(0, 50)}`);
  if (!engineProcess || !engineProcess.stdin) {
    vscode.window.showWarningMessage("Kyrex engine is not running. Start it first.");
    return;
  }
  
  const payloadObj: any = { type: "chat", content: text };
  
  // 1. Try primary active editor
  let doc = vscode.window.activeTextEditor?.document;
  
  // 2. Fallback to first visible editor if active is blank due to focus loss
  if (!doc && vscode.window.visibleTextEditors.length > 0) {
    doc = vscode.window.visibleTextEditors[0].document;
  }
  
  // 3. Ultra-aggressive fallback: look for any open code file in workspace state
  if (!doc) {
    const openDocs = vscode.workspace.textDocuments.filter(d => d.uri.scheme === 'file' && !d.fileName.includes('.git'));
    if (openDocs.length > 0) {
      doc = openDocs[0];
    }
  }

  // Filter: only accept real file documents, not extension-output or other virtual schemes
  if (doc && (doc.uri.scheme !== 'file' || doc.fileName.includes('extension-output'))) {
    doc = undefined;
  }

  output.appendLine(`[DEBUG TRACER] doc check: activeEditor=${!!vscode.window.activeTextEditor}, visible=${vscode.window.visibleTextEditors.length}, open=${vscode.workspace.textDocuments.filter(d => d.uri.scheme === 'file').length}`);
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
          if (engineProcess?.stdin) {
            engineProcess.stdin.write(
              JSON.stringify({ type: "interrupt" }) + "\n"
            );
          }
          break;
        case "clear_ui":
          this.postMessage({ type: "clear_ui" });
          break;
        case "fetch_models":
          this.fetchModels();
          break;
      }
    });
  }

  postMessage(msg: any) {
    this._view?.webview.postMessage(msg);
  }

  private async fetchModels() {
    const config = vscode.workspace.getConfiguration("kyrex");
    const baseUrl: string = config.get("baseUrl", "");
    const apiKey: string = config.get("apiKey", process.env.KYREX_API_KEY || "");
    const provider: string = config.get("provider", "openai");

    if (!baseUrl) {
      this.output.appendLine("[fetchModels] No baseUrl configured — skipping fetch.");
      this.postMessage({ type: "models_list", models: [] });
      return;
    }

    // OpenCode Go serves /models without /v1 prefix; OpenAI standard is /v1/models.
    const urls = [
      baseUrl.replace(/\/+$/, "") + "/models",
      baseUrl.replace(/\/+$/, "") + "/v1/models",
    ];

    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (apiKey) {
      headers["Authorization"] = provider === "anthropic"
        ? "x-api-key " + apiKey
        : "Bearer " + apiKey;
    }

    for (const url of urls) {
      this.output.appendLine(`[fetchModels] Trying: ${url}`);
      try {
        const resp = await fetch(url, { headers, signal: AbortSignal.timeout(8000) });
        this.output.appendLine(`[fetchModels] ${url} -> status ${resp.status} ${resp.statusText}`);

        if (!resp.ok) {
          const bodySnippet = await resp.text().catch(() => "[read failed]");
          this.output.appendLine(`[fetchModels] ${url} -> body: ${bodySnippet.slice(0, 500)}`);
          continue;
        }

        const rawText = await resp.text();
        this.output.appendLine(`[fetchModels] ${url} -> raw response: ${rawText.slice(0, 2000)}`);

        let data: any;
        try {
          data = JSON.parse(rawText);
        } catch {
          this.output.appendLine(`[fetchModels] ${url} -> not valid JSON, skipping.`);
          continue;
        }

        // Try all known model-list shapes
        const raw: any[] = data?.data ?? data?.models ?? data?.model ?? [];
        this.output.appendLine(`[fetchModels] ${url} -> parsed ${raw.length} entries from data/models field`);

        if (!Array.isArray(raw) || raw.length === 0) {
          // Maybe the response IS the array
          if (Array.isArray(data)) {
            this.output.appendLine(`[fetchModels] ${url} -> response is a bare array of ${data.length} entries`);
            raw.push(...data);
          }
        }

        const models: string[] = raw
          .map((m: any) => typeof m === "string" ? m : (m.id || m.name || m.model || ""))
          .filter((id: string) => !!id)
          .sort();

        this.output.appendLine(`[fetchModels] ${url} -> final model list: ${JSON.stringify(models)}`);

        if (models.length > 0) {
          this.postMessage({ type: "models_list", models });
          return;
        }

        this.output.appendLine(`[fetchModels] ${url} -> got response but extracted 0 models, trying next URL...`);
      } catch (err: any) {
        this.output.appendLine(`[fetchModels] ${url} -> error: ${err.message || err}`);
      }
    }

    this.output.appendLine("[fetchModels] All URLs exhausted, no models found.");
    this.postMessage({ type: "models_list", models: [] });
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
      --btn-bg: var(--vscode-button-background);
      --btn-fg: var(--vscode-button-foreground);
      --btn-hover: var(--vscode-button-hoverBackground);
      --err-fg: var(--vscode-errorForeground);
      --desc-fg: var(--vscode-descriptionForeground);
      --editor-bg: var(--vscode-editor-background);
      --badge-bg: var(--vscode-badge-background);
      --badge-fg: var(--vscode-badge-foreground);
      --scrollbar: var(--vscode-scrollbarSlider-background);
      --scrollbar-hover: var(--vscode-scrollbarSlider-hoverBackground);
      --focus-border: var(--vscode-focusBorder);
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
      overflow: hidden;
    }
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--scrollbar); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-hover); }

    /* ── Status Bar ── */
    #status-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 4px 8px;
      border-bottom: 1px solid var(--border);
      font-size: 11px;
      gap: 6px;
      flex-shrink: 0;
    }
    .status-left { display: flex; align-items: center; gap: 6px; min-width: 0; }
    .status-right { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
    .status-dot {
      width: 8px; height: 8px;
      border-radius: 50%;
      flex-shrink: 0;
    }
    .status-dot.online { background: #4ec94e; box-shadow: 0 0 4px #4ec94e88; }
    .status-dot.offline { background: #f14c4c; }
    .status-dot.busy { background: #e0af68; animation: pulse 1s infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
    .status-model {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--fg);
    }
    .mode-badge {
      font-size: 10px;
      padding: 1px 6px;
      border-radius: 3px;
      background: var(--badge-bg);
      color: var(--badge-fg);
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .token-count {
      color: var(--desc-fg);
      font-size: 10px;
      white-space: nowrap;
    }

    /* ── Messages ── */
    #messages {
      flex: 1;
      overflow-y: auto;
      padding: 8px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    #messages:empty::after {
      content: 'Start a conversation with Kyrex.';
      display: block;
      color: var(--desc-fg);
      font-style: italic;
      text-align: center;
      padding: 24px 8px;
      font-size: 13px;
    }

    .msg {
      max-width: 100%;
      animation: fadeIn 0.15s ease;
    }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

    .msg.user {
      align-self: flex-end;
      background: var(--btn-bg);
      color: var(--btn-fg);
      padding: 6px 10px;
      border-radius: 10px 10px 3px 10px;
      white-space: pre-wrap;
      word-break: break-word;
      max-width: 88%;
    }
    .msg-header {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 4px;
    }
    .msg-label {
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.3px;
    }
    .msg-label.assistant-label { color: var(--accent); }
    .msg-label.user-label { color: var(--btn-fg); opacity: 0.8; }

    .msg-body {
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.45;
    }
    .msg-body p { margin-bottom: 6px; }
    .msg-body p:last-child { margin-bottom: 0; }

    .msg.assistant {
      align-self: flex-start;
      padding: 8px 10px;
      border-radius: 10px 10px 10px 3px;
      background: var(--editor-bg);
      border: 1px solid var(--border);
      max-width: 92%;
    }
    .msg.thinking {
      align-self: flex-start;
      color: var(--desc-fg);
      font-style: italic;
      font-size: 0.9em;
      padding: 4px 8px;
    }
    .msg.error {
      align-self: flex-start;
      color: var(--err-fg);
      font-weight: 600;
      padding: 6px 10px;
      background: color-mix(in srgb, var(--err-fg) 8%, transparent);
      border-radius: 6px;
    }

    /* Tool call */
    .tool-call {
      align-self: flex-start;
      width: 100%;
      margin: 2px 0;
    }
    .tool-call-header {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      background: var(--editor-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      cursor: pointer;
      font-size: 12px;
      transition: border-color 0.15s;
    }
    .tool-call-header:hover { border-color: var(--accent); }
    .tool-call-icon { flex-shrink: 0; }
    .tool-call-name { flex: 1; font-weight: 500; }
    .tool-call-status { font-size: 10px; padding: 1px 5px; border-radius: 3px; }
    .tool-call-status.running { color: var(--accent); }
    .tool-call-status.success { color: #4ec94e; }
    .tool-call-status.failed { color: var(--err-fg); }
    .tool-call-chevron { flex-shrink: 0; transition: transform 0.2s; font-size: 10px; }
    .tool-call-chevron.open { transform: rotate(90deg); }
    .tool-result-preview {
      padding: 6px 8px;
      margin: 0 0 0 12px;
      background: var(--input-bg);
      border: 1px solid var(--border);
      border-top: none;
      border-radius: 0 0 6px 6px;
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 11px;
      white-space: pre;
      overflow-x: auto;
      max-height: 150px;
      display: none;
    }
    .tool-result-preview.open { display: block; }

    /* Code block */
    .code-block-wrapper {
      margin: 6px 0;
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
    }
    .code-block-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 3px 8px;
      background: var(--editor-bg);
      border-bottom: 1px solid var(--border);
      font-size: 11px;
      color: var(--desc-fg);
    }
    .code-lang { font-weight: 500; }
    .copy-btn {
      background: none;
      border: 1px solid var(--border);
      color: var(--desc-fg);
      padding: 1px 6px;
      border-radius: 3px;
      cursor: pointer;
      font-size: 10px;
      transition: all 0.15s;
    }
    .copy-btn:hover {
      background: var(--btn-bg);
      color: var(--btn-fg);
      border-color: var(--btn-bg);
    }
    .copy-btn.copied {
      background: #4ec94e;
      color: #1a1b26;
      border-color: #4ec94e;
    }
    .code-block-content {
      padding: 8px;
      background: var(--editor-bg);
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 12px;
      line-height: 1.5;
      overflow-x: auto;
      white-space: pre;
    }

    /* ── Scroll-to-bottom ── */
    #scroll-btn {
      position: sticky;
      bottom: 0;
      align-self: center;
      margin-bottom: 4px;
      width: 28px;
      height: 28px;
      border-radius: 50%;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--accent);
      font-size: 16px;
      cursor: pointer;
      display: none;
      align-items: center;
      justify-content: center;
      transition: all 0.15s;
      z-index: 10;
    }
    #scroll-btn:hover {
      background: var(--btn-bg);
      color: var(--btn-fg);
      border-color: var(--btn-bg);
    }
    #scroll-btn.visible { display: flex; }

    /* ── Input Area ── */
    #input-area {
      flex-shrink: 0;
      border-top: 1px solid var(--border);
      padding: 6px 8px;
      display: flex;
      flex-direction: column;
      gap: 4px;
      background: var(--bg);
    }
    .input-toolbar {
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .toolbar-btn {
      background: none;
      border: none;
      color: var(--desc-fg);
      padding: 2px 6px;
      border-radius: 3px;
      cursor: pointer;
      font-size: 12px;
      transition: all 0.15s;
    }
    .toolbar-btn:hover { color: var(--fg); background: var(--editor-bg); }
    .toolbar-btn.active { color: var(--accent); }
    .input-row {
      display: flex;
      gap: 4px;
      align-items: flex-end;
    }
    #prompt {
      flex: 1;
      resize: none;
      min-height: 28px;
      max-height: 120px;
      padding: 5px 8px;
      background: var(--input-bg);
      color: var(--input-fg);
      border: 1px solid var(--border);
      border-radius: 6px;
      font-family: inherit;
      font-size: inherit;
      line-height: 1.4;
      outline: none;
      transition: border-color 0.15s;
    }
    #prompt:focus { border-color: var(--focus-border); }
    #prompt::placeholder { color: var(--desc-fg); opacity: 0.7; }

    .action-btn {
      padding: 5px 12px;
      border: none;
      border-radius: 6px;
      cursor: pointer;
      font-family: inherit;
      font-size: inherit;
      font-weight: 500;
      transition: all 0.15s;
      white-space: nowrap;
    }
    #send-btn {
      background: var(--btn-bg);
      color: var(--btn-fg);
    }
    #send-btn:hover { background: var(--btn-hover); }
    #send-btn:disabled { opacity: 0.4; cursor: default; }

    #stop-btn {
      background: transparent;
      color: var(--err-fg);
      border: 1px solid var(--err-fg);
      display: none;
    }
    #stop-btn:hover { background: color-mix(in srgb, var(--err-fg) 12%, transparent); }

    .new-session-btn {
      background: none;
      border: 1px solid var(--border);
      color: var(--desc-fg);
      padding: 2px 8px;
      border-radius: 4px;
      cursor: pointer;
      font-size: 11px;
      transition: all 0.15s;
    }
    .new-session-btn:hover { color: var(--fg); border-color: var(--fg); }

    /* ── Settings Panel ── */
    #settings-panel {
      flex-shrink: 0;
      border-top: 1px solid var(--border);
    }
    .settings-toggle {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      cursor: pointer;
      font-size: 11px;
      color: var(--desc-fg);
      user-select: none;
      transition: color 0.15s;
    }
    .settings-toggle:hover { color: var(--fg); }
    .settings-toggle .chevron { transition: transform 0.2s; font-size: 10px; }
    .settings-toggle .chevron.open { transform: rotate(90deg); }
    .settings-body {
      padding: 4px 8px 8px;
      display: none;
      flex-direction: column;
      gap: 6px;
    }
    .settings-body.open { display: flex; }
    .setting-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .setting-row label {
      font-size: 11px;
      color: var(--desc-fg);
      flex-shrink: 0;
    }
    .setting-row select {
      flex: 1;
      padding: 3px 6px;
      background: var(--input-bg);
      color: var(--input-fg);
      border: 1px solid var(--border);
      border-radius: 4px;
      font-family: inherit;
      font-size: 11px;
      outline: none;
      cursor: pointer;
    }
    .setting-row select:focus { border-color: var(--focus-border); }

    /* ── Thinking indicator ── */
    .thinking-indicator {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      color: var(--desc-fg);
      font-style: italic;
      font-size: 12px;
    }
    .thinking-dots::after {
      content: '';
      animation: dots 1.5s steps(4, end) infinite;
    }
    @keyframes dots { 0% { content: ''; } 25% { content: '.'; } 50% { content: '..'; } 75% { content: '...'; } 100% { content: ''; } }
  </style>
</head>
<body>
  <!-- ── Status Bar ── -->
  <div id="status-bar">
    <div class="status-left">
      <span class="status-dot online" id="status-dot"></span>
      <span class="status-model" id="status-model">Kyrex</span>
    </div>
    <div class="status-right">
      <span class="mode-badge" id="mode-badge">PLAN</span>
      <span class="token-count" id="token-count">0 tokens</span>
    </div>
  </div>

  <!-- ── Messages ── -->
  <div id="messages"></div>

  <!-- ── Scroll-to-bottom ── -->
  <button id="scroll-btn">↓</button>

  <!-- ── Input Area ── -->
  <div id="input-area">
    <div class="input-toolbar">
      <button class="toolbar-btn" id="attach-btn" title="Attach File">📎</button>
      <button class="new-session-btn" id="new-session-btn" title="New Session">+ New</button>
    </div>
    <div class="input-row">
      <textarea id="prompt" rows="1" placeholder="Ask Kyrex..."></textarea>
      <button class="action-btn" id="send-btn">Send</button>
      <button class="action-btn" id="stop-btn">■ Stop</button>
    </div>
  </div>

  <!-- ── Settings Panel ── -->
  <div id="settings-panel">
    <div class="settings-toggle" id="settings-toggle">
      <span class="chevron" id="settings-chevron">▶</span>
      <span>Settings</span>
    </div>
    <div class="settings-body" id="settings-body">
      <div class="setting-row">
        <label>Model</label>
        <select id="model-select">
          <option value="">Loading...</option>
        </select>
      </div>
      <div class="setting-row">
        <label>Provider</label>
        <select id="provider-select">
          <option value="openai">OpenAI</option>
          <option value="anthropic">Anthropic</option>
        </select>
      </div>
    </div>
  </div>

  <script>
    const vscode = acquireVsCodeApi();

    // ── DOM Refs ──
    const messagesEl = document.getElementById('messages');
    const promptEl = document.getElementById('prompt');
    const sendBtn = document.getElementById('send-btn');
    const stopBtn = document.getElementById('stop-btn');
    const scrollBtn = document.getElementById('scroll-btn');
    const statusDot = document.getElementById('status-dot');
    const statusModel = document.getElementById('status-model');
    const modeBadge = document.getElementById('mode-badge');
    const tokenCount = document.getElementById('token-count');
    const settingsToggle = document.getElementById('settings-toggle');
    const settingsBody = document.getElementById('settings-body');
    const settingsChevron = document.getElementById('settings-chevron');
    const modelSelect = document.getElementById('model-select');
    const providerSelect = document.getElementById('provider-select');
    const attachBtn = document.getElementById('attach-btn');
    const newSessionBtn = document.getElementById('new-session-btn');

    // ── State ──
    let isGenerating = false;
    let currentAssistantEl = null;
    let currentThinkingEl = null;
    let pendingToolCalls = [];
    let scrollCheckInterval = null;
    let sessionTokens = 0;

    // ── Utility ──
    function escapeHtml(text) {
      const div = document.createElement('div');
      div.textContent = text;
      return div.innerHTML;
    }

    function scrollToBottom(smooth) {
      messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    }

    function checkScroll() {
      const threshold = 80;
      const atBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < threshold;
      scrollBtn.classList.toggle('visible', !atBottom);
    }

    // ── Status Bar ──
    function setEngineStatus(status) {
      statusDot.className = 'status-dot ' + status;
    }
    function setModel(name) {
      statusModel.textContent = name || 'Kyrex';
    }
    function setMode(mode) {
      modeBadge.textContent = (mode || 'PLAN').toUpperCase();
    }
    function setTokens(count) {
      sessionTokens = count || 0;
      tokenCount.textContent = (count || 0).toLocaleString() + ' tokens';
    }
    function addTokens(n) {
      setTokens(sessionTokens + (n || 0));
    }

    // ── Message Bubbles ──
    function addMessage(type, content, extra) {
      const div = document.createElement('div');

      if (type === 'user') {
        div.className = 'msg user';
        div.textContent = content;
      } else if (type === 'assistant') {
        div.className = 'msg assistant';
        const header = document.createElement('div');
        header.className = 'msg-header';
        const label = document.createElement('span');
        label.className = 'msg-label assistant-label';
        label.textContent = 'Kyrex';
        header.appendChild(label);
        div.appendChild(header);

        const body = document.createElement('div');
        body.className = 'msg-body';
        body.innerHTML = renderMarkdown(content);
        div.appendChild(body);
      } else if (type === 'thinking') {
        div.className = 'msg thinking';
        div.innerHTML = '<span>\u{1F4AD} Thinking' + (content || '') + '</span>';
      } else if (type === 'error') {
        div.className = 'msg error';
        div.textContent = '\u26A0 ' + content;
      }

      if (extra && extra.id) div.dataset.id = extra.id;
      messagesEl.appendChild(div);
      checkScroll();
      if (scrollBtn.classList.contains('visible')) {
        // Don't auto-scroll if user scrolled up
      } else {
        scrollToBottom(false);
      }
      return div;
    }

    // ── Markdown Renderer (lightweight) ──
    function renderMarkdown(text) {
      if (!text) return '';
      let html = escapeHtml(text);

      // Code blocks: split by triple backtick fences
      var codeParts = html.split('\`\`\`');
      if (codeParts.length > 1) {
        var rebuilt = '';
        for (var ci = 0; ci < codeParts.length; ci++) {
          if (ci % 2 === 0) {
            rebuilt += codeParts[ci];
          } else {
            var clines = codeParts[ci].split('\\n');
            var clang = clines[0].trim();
            var ccode = clines.slice(1).join('\\n');
            var cleanCcode = ccode.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
            rebuilt += '<div class="code-block-wrapper">' +
              '<div class="code-block-header">' +
              '<span class="code-lang">' + escapeHtml(clang || 'code') + '</span>' +
              '<button class="copy-btn" onclick="copyCode(this)" data-code="' + escapeHtml(cleanCcode) + '">Copy</button>' +
              '</div>' +
              '<div class="code-block-content">' + escapeHtml(ccode) + '</div>' +
              '</div>';
          }
        }
        html = rebuilt;
      }

      // Inline code (single backticks)
      html = html.replace(new RegExp('\`([^\`]+)\`', 'g'), '<code style="background:var(--input-bg);padding:1px 4px;border-radius:3px;font-size:0.9em;font-family:var(--vscode-editor-font-family,monospace)">$1</code>');

      // Bold
      html = html.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');

      // Italic
      html = html.replace(/\\*([^*]+)\\*/g, '<em>$1</em>');

      // Line breaks
      html = html.replace(/\\n/g, '<br>');

      return html;
    }

    function copyCode(btn) {
      const code = btn.dataset.code;
      navigator.clipboard.writeText(code).then(() => {
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => {
          btn.textContent = 'Copy';
          btn.classList.remove('copied');
        }, 1500);
      }).catch(() => {
        // Fallback
        const ta = document.createElement('textarea');
        ta.value = code;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => {
          btn.textContent = 'Copy';
          btn.classList.remove('copied');
        }, 1500);
      });
    }

    // ── Tool Calls ──
    function addToolCall(name, id) {
      const div = document.createElement('div');
      div.className = 'tool-call';
      div.dataset.toolId = id;

      const header = document.createElement('div');
      header.className = 'tool-call-header';
      header.innerHTML = '<span class="tool-call-icon">\u{1F527}</span>' +
        '<span class="tool-call-name">' + escapeHtml(name) + '</span>' +
        '<span class="tool-call-status running">running</span>' +
        '<span class="tool-call-chevron">\u25B6</span>';

      const preview = document.createElement('div');
      preview.className = 'tool-result-preview';
      preview.textContent = 'Waiting for result...';

      header.addEventListener('click', () => {
        preview.classList.toggle('open');
        header.querySelector('.tool-call-chevron').classList.toggle('open');
      });

      div.appendChild(header);
      div.appendChild(preview);
      messagesEl.appendChild(div);
      scrollToBottom(false);
      return { el: div, header, preview, statusEl: header.querySelector('.tool-call-status') };
    }

    function updateToolCall(id, status, result) {
      const el = messagesEl.querySelector('.tool-call[data-tool-id="' + id + '"]');
      if (!el) return;
      const header = el.querySelector('.tool-call-header');
      const statusSpan = header.querySelector('.tool-call-status');
      const preview = el.querySelector('.tool-result-preview');

      statusSpan.className = 'tool-call-status ' + status;
      statusSpan.textContent = status;

      if (result) {
        preview.textContent = typeof result === 'string' ? result : JSON.stringify(result, null, 2);
      }
      checkScroll();
    }

    // ── Send / Stop ──
    function send() {
      const text = promptEl.value.trim();
      if (!text || isGenerating) return;

      addMessage('user', text);
      promptEl.value = '';
      promptEl.style.height = 'auto';
      setGenerating(true);
      vscode.postMessage({ type: 'send', text });
    }

    function setGenerating(generating) {
      isGenerating = generating;
      sendBtn.style.display = generating ? 'none' : 'inline-block';
      stopBtn.style.display = generating ? 'inline-block' : 'none';
      sendBtn.disabled = generating;
      if (generating) {
        setEngineStatus('busy');
      } else {
        setEngineStatus('online');
      }
      // Start/stop scroll check
      if (generating) {
        if (!scrollCheckInterval) {
          scrollCheckInterval = setInterval(checkScroll, 300);
        }
      } else {
        if (scrollCheckInterval) {
          clearInterval(scrollCheckInterval);
          scrollCheckInterval = null;
          checkScroll();
        }
      }
    }

    function stopGeneration() {
      if (!isGenerating) return;
      vscode.postMessage({ type: 'interrupt' });
      setGenerating(false);
    }

    // ── Auto-resize Textarea ──
    promptEl.addEventListener('input', () => {
      promptEl.style.height = 'auto';
      promptEl.style.height = Math.min(promptEl.scrollHeight, 120) + 'px';
    });

    // ── Send on Enter ──
    promptEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });

    // ── Button Listeners ──
    sendBtn.addEventListener('click', send);
    stopBtn.addEventListener('click', stopGeneration);

    newSessionBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'send', text: '/clear' });
      vscode.postMessage({ type: 'clear_ui' });
      messagesEl.innerHTML = '';
      currentAssistantEl = null;
      pendingToolCalls = [];
      setTokens(0);
    });

    // ── Settings Panel ──
    settingsToggle.addEventListener('click', () => {
      const isOpen = settingsBody.classList.toggle('open');
      settingsChevron.classList.toggle('open', isOpen);
      if (isOpen) vscode.postMessage({ type: 'fetch_models' });
    });

    modelSelect.addEventListener('change', () => {
      vscode.postMessage({ type: 'send', text: '/model ' + modelSelect.value });
    });

    providerSelect.addEventListener('change', () => {
      vscode.postMessage({ type: 'send', text: '/provider ' + providerSelect.value });
    });

    // ── Attach File ──
    attachBtn.addEventListener('click', () => {
      const input = document.createElement('input');
      input.type = 'file';
      input.addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (ev) => {
          const content = ev.target.result;
          const path = file.name;
          vscode.postMessage({ type: 'send', text: 'Attached file: ' + path });
          // The engine needs to know about this file - send as context
          vscode.postMessage({ type: 'send', text: '/context file://' + path });
        };
        reader.readAsText(file);
      });
      input.click();
    });

    // ── Scroll-to-bottom ──
    scrollBtn.addEventListener('click', () => scrollToBottom(true));
    messagesEl.addEventListener('scroll', checkScroll);

    // ── Window Messages ──
    window.addEventListener('message', (e) => {
      const msg = e.data;
      if (!msg) return;

      switch (msg.type) {
        case 'engine': {
          const p = msg.payload;
          if (!p) break;

          switch (p.type) {
            case 'token': {
              if (p.content) {
                if (!currentAssistantEl) {
                  currentAssistantEl = addMessage('assistant', '');
                }
                const body = currentAssistantEl.querySelector('.msg-body');
                if (body) {
                  body.innerHTML = renderMarkdown(body.textContent + p.content);
                } else {
                  currentAssistantEl.textContent += p.content;
                }
                addTokens(1);
              }
              break;
            }
            case 'reasoning': {
              const text = p.content || p.reasoning || '';
              if (text) {
                if (!currentThinkingEl) {
                  currentThinkingEl = addMessage('thinking', '');
                }
                currentThinkingEl.innerHTML = '<span>\u{1F4AD} ' + escapeHtml(text) + '</span>';
              }
              break;
            }
            case 'chat_done': {
              if (currentThinkingEl) {
                currentThinkingEl.remove();
                currentThinkingEl = null;
              }
              currentAssistantEl = null;
              setGenerating(false);
              setEngineStatus('online');
              break;
            }
            case 'tool_start': {
              const toolId = p.id || 'tool_' + Date.now();
              const toolName = p.name || 'tool';
              pendingToolCalls.push(toolId);
              addToolCall(toolName, toolId);
              // Convert thinking to tool call if present
              if (currentThinkingEl) {
                currentThinkingEl.remove();
                currentThinkingEl = null;
              }
              break;
            }
            case 'tool_result': {
              const toolId = p.id || pendingToolCalls.shift();
              if (toolId) {
                const result = p.result || p.content || 'OK';
                updateToolCall(toolId, 'success', result);
              }
              break;
            }
            case 'error': {
              addMessage('error', p.content || 'Unknown error');
              setGenerating(false);
              break;
            }
            case 'session_state': {
              setModel(p.model || 'Kyrex');
              setEngineStatus('online');
              if (p.mode) setMode(p.mode);
              if (p.tokens != null) setTokens(Number(p.tokens));
              if (p.provider && providerSelect) {
                const opt = Array.from(providerSelect.options).find(o => o.value === p.provider);
                if (opt) providerSelect.value = p.provider;
              }
              break;
            }
            case 'phase': {
              if (p.value === 'IDLE' || p.value === 'PLAN' || p.value === 'EXECUTE') {
                // Track phase
              }
              if (p.value === 'IDLE') {
                currentAssistantEl = null;
                setGenerating(false);
                setEngineStatus('online');
              }
              if (p.value === 'PLAN') setMode('plan');
              if (p.value === 'EXECUTE') setMode('execute');
              break;
            }
            case 'tui_pause': {
              // Handle model list from engine
              if (p.value === 'model_picker' && p.files) {
                const models = Array.isArray(p.files) ? p.files : [];
                modelSelect.innerHTML = '';
                models.forEach(m => {
                  const opt = document.createElement('option');
                  opt.value = m;
                  opt.textContent = m;
                  modelSelect.appendChild(opt);
                });
              }
              break;
            }
            default:
              break;
          }
          break;
        }
        case 'engine_status': {
          const running = msg.payload && msg.payload.running;
          setEngineStatus(running ? 'online' : 'offline');
          setModel(running ? 'Kyrex' : 'Disconnected');
          if (!running) setGenerating(false);
          break;
        }
        case 'clear_ui': {
          messagesEl.innerHTML = '';
          currentAssistantEl = null;
          currentThinkingEl = null;
          pendingToolCalls = [];
          setTokens(0);
          break;
        }
        case 'models_list': {
          const models = Array.isArray(msg.models) ? msg.models : [];
          modelSelect.innerHTML = '';
          if (models.length === 0) {
            const opt = document.createElement('option');
            opt.value = '';
            opt.textContent = 'No models found';
            modelSelect.appendChild(opt);
          } else {
            models.forEach(m => {
              const opt = document.createElement('option');
              opt.value = m;
              opt.textContent = m;
              modelSelect.appendChild(opt);
            });
          }
          break;
        }
      }
    });

    // ── Init ──
    setEngineStatus('offline');
    setMode('plan');
    setTokens(0);
    sendBtn.style.display = 'inline-block';
    stopBtn.style.display = 'none';
    vscode.postMessage({ type: 'fetch_models' });
  </script>
</body>
</html>`;
  }
}