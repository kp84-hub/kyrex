import * as vscode from "vscode";
import { ChildProcess, spawn } from "child_process";
import * as os from "os";
import * as path from "path";
import * as fs from "fs";

let engineProcess: ChildProcess | null = null;

// ── Edit Queue for VS Code propose_edit protocol ──
interface EditProposal {
  editId: string;
  filePath: string;
  content: string;
  tmpFile: string;
}

class EditQueue {
  private queue: EditProposal[] = [];
  private showing = false;
  private autoAcceptAll = false;
  private output: vscode.OutputChannel;
  private sidebarProvider: KyrexSidebarProvider | null = null;

  constructor(output: vscode.OutputChannel) {
    this.output = output;
  }
  
  setSidebarProvider(provider: KyrexSidebarProvider) {
    this.sidebarProvider = provider;
  }

  enqueue(proposal: EditProposal) {
    this.queue.push(proposal);
    this.output.appendLine(`[EditQueue] Enqueued edit ${proposal.editId} for ${proposal.filePath}`);
    if (!this.showing) {
      this.showNext();
    }
  }

  async showNext() {
    // Check autoAcceptAll at the very top
    if (this.autoAcceptAll && this.queue.length > 0) {
      this.output.appendLine(`[EditQueue] autoAcceptAll active — auto-accepting ${this.queue[0].editId}`);
      this.accept();
      return;
    }
    
    if (this.queue.length === 0) {
      this.showing = false;
      this.autoAcceptAll = false;
      this.output.appendLine(`[EditQueue] Queue empty — autoAcceptAll reset`);
      return;
    }
    this.showing = true;
    const proposal = this.queue[0];
    this.output.appendLine(`[EditQueue] Showing edit ${proposal.editId}`);

    // Open diff view
    const originalUri = vscode.Uri.file(proposal.filePath);
    const modifiedUri = vscode.Uri.file(proposal.tmpFile);
    const base = path.basename(proposal.filePath);
    const title = `Kyrex: ${base}`;

    try {
      await vscode.commands.executeCommand("vscode.diff", originalUri, modifiedUri, title);
    } catch (e: any) {
      this.output.appendLine(`[EditQueue] diff error: ${e.message}`);
    }

    // ── Trust Mode: auto-accept after delay ──
    const config = vscode.workspace.getConfiguration("kyrex");
    const trustMode: boolean = config.get("trustMode", false);
    if (trustMode) {
      this.output.appendLine(`[EditQueue] trustMode ON — auto-accepting in 2.5s`);
      setTimeout(() => {
        this.accept();
      }, 2500);
      return;
    }

    // Non-blocking notification with buttons
    const result = await vscode.window.showInformationMessage(
      `Apply this change to ${base}?`,
      "Accept",
      "Reject",
      "Accept All"
    );

    if (result === "Accept") {
      this.accept();
    } else if (result === "Reject") {
      this.reject();
    } else if (result === "Accept All") {
      this.acceptAll();
    } else {
      // User dismissed — treat as reject
      this.reject();
    }
  }

  accept() {
    if (this.queue.length === 0) return;
    const proposal = this.queue.shift()!;
    this.output.appendLine(`[EditQueue] Accepted edit ${proposal.editId}`);
    this.sendDecision(proposal.editId, true);
    this.cleanupTemp(proposal.tmpFile);
    this.showNext();
  }

  reject() {
    if (this.queue.length === 0) return;
    const proposal = this.queue.shift()!;
    this.output.appendLine(`[EditQueue] Rejected edit ${proposal.editId}`);
    this.sendDecision(proposal.editId, false);
    this.cleanupTemp(proposal.tmpFile);
    this.showNext();
  }

  acceptAll() {
    this.output.appendLine(`[EditQueue] acceptAll() — setting autoAcceptAll flag (${this.queue.length} pending)`);
    this.autoAcceptAll = true;
    // Accept the current edit; showNext() will auto-accept the rest via the flag
    this.accept();
  }

  private sendDecision(editId: string, accepted: boolean) {
    if (engineProcess?.stdin) {
      const payload = JSON.stringify({
        type: "edit_decision",
        editId: editId,
        accepted: accepted
      }) + "\n";
      engineProcess.stdin.write(payload);
      this.output.appendLine(`[EditQueue] Sent decision: ${editId} accepted=${accepted}`);
    }
    
    // Notify sidebar of decision
    if (this.sidebarProvider) {
      this.sidebarProvider.postMessage({
        type: "edit_decision",
        editId: editId,
        accepted: accepted
      });
    }
  }

  private cleanupTemp(tmpFile: string) {
    try {
      fs.unlinkSync(tmpFile);
    } catch {}
  }

  dispose() {
    // Clean up any remaining temp files on deactivation
    for (const proposal of this.queue) {
      this.cleanupTemp(proposal.tmpFile);
    }
    this.queue = [];
  }
}

let editQueue: EditQueue | null = null;

export function activate(context: vscode.ExtensionContext) {
  const outputChannel = vscode.window.createOutputChannel("Kyrex Engine");
  outputChannel.appendLine("Kyrex VS Code extension activated.");

  // ── Initialize edit queue ──────────────────────────────────────
  editQueue = new EditQueue(outputChannel);

  // ── Register sidebar webview provider ──────────────────────────
  const sidebarProvider = new KyrexSidebarProvider(context.extensionUri, outputChannel);
  editQueue.setSidebarProvider(sidebarProvider);
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

  // ── Command: Accept Edit ───────────────────────────────────────
  const acceptEditCmd = vscode.commands.registerCommand("kyrex.acceptEdit", () => {
    editQueue?.accept();
  });
  context.subscriptions.push(acceptEditCmd);

  // ── Command: Reject Edit ───────────────────────────────────────
  const rejectEditCmd = vscode.commands.registerCommand("kyrex.rejectEdit", () => {
    editQueue?.reject();
  });
  context.subscriptions.push(rejectEditCmd);

  // ── Command: Accept All Edits ──────────────────────────────────
  const acceptAllEditsCmd = vscode.commands.registerCommand("kyrex.acceptAllEdits", () => {
    editQueue?.acceptAll();
  });
  context.subscriptions.push(acceptAllEditsCmd);

  // ── Auto-start engine on activation ────────────────────────────
  startEngine(context, outputChannel, sidebarProvider);
}

export function deactivate() {
  editQueue?.dispose();
  editQueue = null;
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

  // Use custom engine path if set, otherwise use bundled engine
  const customEnginePath: string = config.get("enginePath", "");
  const bridgeScript = customEnginePath || path.join(context.extensionPath, "kyrex_engine", "core_bridge.py");

  output.appendLine(`Starting engine: ${pythonPath} ${bridgeScript}`);

  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;

  engineProcess = spawn(pythonPath, [bridgeScript], {
    cwd: workspaceRoot,
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

  let lineBuffer = '';

  engineProcess.stdout?.on("data", (data: Buffer) => {
    lineBuffer += data.toString();
    const lines = lineBuffer.split("\n");
    lineBuffer = lines.pop()!; // keep incomplete tail for next chunk
    for (const line of lines.filter((l: any) => l.trim())) {
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
          // Also forward to sidebar for display
          sidebarProvider.postMessage({ type: "engine", payload: msg });
          continue;
        }
        
        // ── 1.6 FORWARD EDIT_DECISION TO SIDEBAR ──
        if (msg.type === "edit_decision") {
          sidebarProvider.postMessage({ type: "engine", payload: msg });
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

function handleProposeEdit(
  msg: { editId: string; filePath: string; content: string },
  output: vscode.OutputChannel
) {
  const { editId, filePath, content } = msg;
  output.appendLine(`[propose_edit] Incoming edit ${editId} for: ${filePath}`);

  if (!editQueue) {
    output.appendLine(`[propose_edit] ERROR: EditQueue not initialized`);
    return;
  }

  // Write proposed content to a temp file (preserving extension for syntax highlighting)
  const tmpDir = os.tmpdir();
  const base = path.basename(filePath);
  const tmpFile = path.join(tmpDir, `kyrex-${editId}-${base}`);
  fs.writeFileSync(tmpFile, content, "utf-8");
  output.appendLine(`[propose_edit] Temp file: ${tmpFile}`);

  // Enqueue for non-blocking sequential review
  editQueue.enqueue({ editId, filePath, content, tmpFile });
}

async function stopEngine(output?: vscode.OutputChannel) {
  if (engineProcess) {
    engineProcess.kill();
    engineProcess = null;
    output?.appendLine("Engine stopped.");
  }
}

function scanWorkspaceTree(rootPath: string, maxDepth: number = 3, maxFiles: number = 200): string {
  const IGNORE_DIRS = new Set([
    'node_modules', '.git', '.svn', '.hg', 'dist', 'build', 'out', 'bin',
    '.next', '.nuxt', '__pycache__', '.venv', 'venv', '.tox', 'target',
    '.idea', '.vs', 'coverage', '.nyc_output', '.cache', '.parcel-cache',
    'vendor', '.gradle', '.maven', 'Pods', '.dart_tool', '.pub-cache'
  ]);

  const lines: string[] = [];
  let fileCount = 0;

  function walk(dir: string, prefix: string, depth: number) {
    if (depth > maxDepth || fileCount >= maxFiles) return;

    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }

    // Sort: directories first, then files, alphabetical within each group
    entries.sort((a, b) => {
      if (a.isDirectory() && !b.isDirectory()) return -1;
      if (!a.isDirectory() && b.isDirectory()) return 1;
      return a.name.localeCompare(b.name);
    });

    for (const entry of entries) {
      if (fileCount >= maxFiles) break;
      if (entry.name.startsWith('.') && entry.name !== '.env' && entry.name !== '.gitignore') continue;
      if (entry.isDirectory() && IGNORE_DIRS.has(entry.name)) continue;

      if (entry.isDirectory()) {
        lines.push(`${prefix}${entry.name}/`);
        walk(path.join(dir, entry.name), prefix + '  ', depth + 1);
      } else {
        lines.push(`${prefix}${entry.name}`);
        fileCount++;
      }
    }
  }

  const rootName = path.basename(rootPath);
  lines.push(`${rootName}/`);
  walk(rootPath, '  ', 1);

  if (fileCount >= maxFiles) {
    lines.push(`  ... (truncated at ${maxFiles} files)`);
  }

  return lines.join('\n');
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
    // No editor tabs open — fall back to scanning workspace folder structure
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (workspaceFolders && workspaceFolders.length > 0) {
      const rootPath = workspaceFolders[0].uri.fsPath;
      output.appendLine(`[DEBUG TRACER] No open editors — scanning workspace: ${rootPath}`);
      const tree = scanWorkspaceTree(rootPath);
      payloadObj.workspaceStructure = {
        root: rootPath,
        name: workspaceFolders[0].name,
        tree: tree
      };
      output.appendLine(`[DEBUG TRACER] Injected workspace tree (${tree.split('\n').length} lines)`);
    } else {
      output.appendLine(`[DEBUG TRACER] WARNING: No open editors and no workspace folder open.`);
    }
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
    .msg.assistant.narration {
      border-left: 3px solid var(--accent);
    }
    .msg-label.kyrex-label {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 1px;
      color: var(--accent);
      margin-bottom: 6px;
      padding-bottom: 4px;
      border-bottom: 1px solid var(--border);
    }
    .kyrex-badge {
      background: var(--accent);
      color: var(--editor-bg);
      padding: 2px 6px;
      border-radius: 3px;
      font-size: 10px;
      font-weight: 800;
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

    /* Tool call - subtle inline indicator */
    .tool-call {
      align-self: flex-start;
      width: 100%;
      margin: 4px 0 4px 8px;
      padding-left: 12px;
      border-left: 2px solid var(--border);
    }
    .tool-call-indicator {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 11px;
      color: var(--desc-fg);
      padding: 2px 0;
    }
    .tool-call-icon { 
      flex-shrink: 0; 
      font-size: 12px;
      opacity: 0.7;
    }
    .tool-call-label { 
      font-weight: 500;
      opacity: 0.8;
    }
    .tool-call-file { 
      color: var(--fg);
      font-weight: 400;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tool-call-status { 
      font-size: 10px;
      margin-left: auto;
    }
    .tool-call-status.running { 
      color: var(--accent);
      animation: pulse 1s infinite;
    }
    .tool-call-status.success { color: #4ec94e; }
    .tool-call-status.failed { color: var(--err-fg); }
    
    /* Edit card - shows filename and line delta */
    .edit-card {
      align-self: flex-start;
      width: 100%;
      margin: 6px 0;
      padding: 8px 10px;
      background: var(--editor-bg);
      border: 1px solid var(--border);
      border-left: 3px solid var(--accent);
      border-radius: 4px;
      cursor: pointer;
      transition: all 0.15s;
    }
    .edit-card:hover {
      border-color: var(--accent);
      background: color-mix(in srgb, var(--accent) 5%, var(--editor-bg));
    }
    .edit-card-header {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
    }
    .edit-card-icon { font-size: 14px; }
    .edit-card-filename {
      flex: 1;
      font-weight: 500;
      color: var(--fg);
    }
    .edit-card-delta {
      font-size: 11px;
      color: var(--desc-fg);
      font-family: var(--vscode-editor-font-family, monospace);
    }
    .edit-card-delta .add { color: #9ece6a; }
    .edit-card-delta .remove { color: #f7768e; }

    /* Research group - subtle collapsed indicator */
    .research-group {
      align-self: flex-start;
      width: 100%;
      margin: 4px 0 4px 8px;
      padding-left: 12px;
      border-left: 2px solid var(--border);
    }
    .research-header {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 2px 0;
      cursor: pointer;
      font-size: 11px;
      color: var(--desc-fg);
      transition: color 0.15s;
    }
    .research-header:hover { 
      color: var(--fg);
    }
    .research-icon { 
      flex-shrink: 0; 
      font-size: 12px;
      opacity: 0.7;
    }
    .research-label { 
      font-weight: 500;
      opacity: 0.8;
    }
    .research-count {
      font-size: 10px;
      padding: 1px 5px;
      border-radius: 8px;
      background: color-mix(in srgb, var(--accent) 15%, transparent);
      color: var(--accent);
      font-weight: 600;
    }
    .research-details {
      margin: 2px 0 0 0;
      padding: 0;
      display: none;
    }
    .research-details.open { display: block; }
    .research-item {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 2px 0;
      font-size: 11px;
      color: var(--desc-fg);
      opacity: 0.8;
    }
    .research-item-icon { flex-shrink: 0; font-size: 11px; }
    .research-item-name { font-weight: 500; }
    .research-item-file { 
      color: var(--fg);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

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
    let streamingBuffer = '';

    // ── Utility ──
    function escapeHtml(text) {
      return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
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
    const RESEARCH_TOOLS = ['search', 'read_local_file', 'run_command', 'list_local_files'];
    const EDIT_TOOLS = ['edit_file', 'write_file'];
    
    // Tool icons and labels
    const TOOL_META = {
      'search': { icon: '🔍', label: 'Search' },
      'read_local_file': { icon: '📄', label: 'Read' },
      'run_command': { icon: '⚡', label: 'Run' },
      'list_local_files': { icon: '📁', label: 'List' },
      'edit_file': { icon: '✏️', label: 'Edit' },
      'write_file': { icon: '💾', label: 'Write' },
      'read_file': { icon: '📄', label: 'Read' },
      'query_memory': { icon: '🧠', label: 'Memory' },
      'query_knowledge': { icon: '📚', label: 'Knowledge' }
    };
    
    // State for research group collapsing
    let currentResearchGroup = null;
    
    function getToolMeta(name) {
      return TOOL_META[name] || { icon: '🔧', label: name };
    }
    
    function extractFilename(args) {
      if (!args) return '';
      const p = args.path || args.file || args.directory || '';
      if (!p) return '';
      const parts = p.split(/[\\\\/]/);
      return parts[parts.length - 1] || p;
    }
    
    function isResearchTool(name) {
      return RESEARCH_TOOLS.includes(name);
    }
    
    function isEditTool(name) {
      return EDIT_TOOLS.includes(name);
    }
    
    function addToolCall(name, id, args) {
      const meta = getToolMeta(name);
      const filename = extractFilename(args);
      
      // If it's a research tool, add to research group
      if (isResearchTool(name)) {
        return addToResearchGroup(name, id, filename, meta);
      }
      
      // Close any open research group
      closeResearchGroup();
      
      // Edit tools get special edit cards
      if (isEditTool(name) && filename) {
        return addEditCard(name, id, filename, args, meta);
      }
      
      // Regular tool - subtle inline indicator
      const div = document.createElement('div');
      div.className = 'tool-call';
      div.dataset.toolId = id;
      div.dataset.toolName = name;

      const indicator = document.createElement('div');
      indicator.className = 'tool-call-indicator';
      
      let html = '<span class="tool-call-icon">' + meta.icon + '</span>' +
        '<span class="tool-call-label">' + meta.label + '</span>';
      if (filename) {
        html += '<span class="tool-call-file">' + escapeHtml(filename) + '</span>';
      }
      html += '<span class="tool-call-status running">●</span>';
      
      indicator.innerHTML = html;
      div.appendChild(indicator);
      messagesEl.appendChild(div);
      scrollToBottom(false);
      return { el: div, indicator, statusEl: indicator.querySelector('.tool-call-status') };
    }
    
    function addEditCard(name, id, filename, args, meta) {
      // Check for existing card by filePath — update in place instead of creating new row
      const filePath = args.path || '';
      if (filePath) {
        const existing = messagesEl.querySelector('.edit-card[data-file-path="' + CSS.escape(filePath) + '"]');
        if (existing) {
          // Update existing card: bump tool id, reset to running status
          existing.dataset.toolId = id;
          existing.dataset.toolName = name;
          const statusSpan = existing.querySelector('.tool-call-status');
          if (statusSpan) {
            statusSpan.className = 'tool-call-status running';
            statusSpan.textContent = '●';
          }
          // Update delta if provided
          const deltaEl = existing.querySelector('.edit-card-delta');
          if (deltaEl && args.old_content && args.new_content) {
            const oldLines = args.old_content.split('\\n').length;
            const newLines = args.new_content.split('\\n').length;
            const diff = newLines - oldLines;
            const diffStr = diff > 0 ? '+' + diff : diff.toString();
            const diffClass = diff >= 0 ? 'add' : 'remove';
            deltaEl.innerHTML = '<span class="' + diffClass + '">' + diffStr + ' lines</span>';
          }
          scrollToBottom(false);
          return { el: existing, indicator: existing.querySelector('.edit-card-header'), statusEl: existing.querySelector('.tool-call-status') };
        }
      }

      const div = document.createElement('div');
      div.className = 'edit-card';
      div.dataset.toolId = id;
      div.dataset.toolName = name;
      div.dataset.filePath = filePath;

      const header = document.createElement('div');
      header.className = 'edit-card-header';
      
      // Calculate line delta if we have old/new content
      let deltaHtml = '';
      if (args.old_content && args.new_content) {
        const oldLines = args.old_content.split('\\n').length;
        const newLines = args.new_content.split('\\n').length;
        const diff = newLines - oldLines;
        const diffStr = diff > 0 ? '+' + diff : diff.toString();
        deltaHtml = '<span class="edit-card-delta">' +
          '<span class="' + (diff >= 0 ? 'add' : 'remove') + '">' + diffStr + ' lines</span></span>';
      }
      
      header.innerHTML = '<span class="edit-card-icon">' + meta.icon + '</span>' +
        '<span class="edit-card-filename" title="' + escapeHtml(filePath) + '">' + escapeHtml(filename) + '</span>' +
        deltaHtml +
        '<span class="tool-call-status running">●</span>';

      div.appendChild(header);
      messagesEl.appendChild(div);
      scrollToBottom(false);
      return { el: div, indicator: header, statusEl: header.querySelector('.tool-call-status') };
    }
    
    function addToResearchGroup(name, id, filename, meta) {
      // Create group if it doesn't exist
      if (!currentResearchGroup) {
        const group = document.createElement('div');
        group.className = 'research-group';
        group.dataset.researchGroup = 'active';
        
        const header = document.createElement('div');
        header.className = 'research-header';
        header.innerHTML = '<span class="research-icon">🔍</span>' +
          '<span class="research-label">Research</span>' +
          '<span class="research-count">1</span>';
        
        const details = document.createElement('div');
        details.className = 'research-details';
        
        header.addEventListener('click', () => {
          details.classList.toggle('open');
        });
        
        group.appendChild(header);
        group.appendChild(details);
        messagesEl.appendChild(group);
        
        currentResearchGroup = {
          el: group,
          header: header,
          details: details,
          count: 1,
          items: []
        };
      } else {
        currentResearchGroup.count++;
        currentResearchGroup.header.querySelector('.research-count').textContent = currentResearchGroup.count;
      }
      
      // Add item to details
      const item = document.createElement('div');
      item.className = 'research-item';
      item.dataset.toolId = id;
      item.innerHTML = '<span class="research-item-icon">' + meta.icon + '</span>' +
        '<span class="research-item-name">' + meta.label + '</span>' +
        (filename ? '<span class="research-item-file">' + escapeHtml(filename) + '</span>' : '');
      
      currentResearchGroup.details.appendChild(item);
      currentResearchGroup.items.push({ id, name, el: item });
      
      scrollToBottom(false);
      return { el: item, indicator: currentResearchGroup.header, statusEl: null, isResearch: true };
    }
    
    function closeResearchGroup() {
      if (currentResearchGroup) {
        currentResearchGroup = null;
      }
    }

    function updateToolCall(id, status, result) {
      // First check if it's in a research group
      const researchItem = document.querySelector('.research-item[data-tool-id="' + id + '"]');
      if (researchItem) {
        if (status === 'success') {
          researchItem.style.opacity = '0.5';
        }
        return;
      }
      
      // Check for edit card
      let el = messagesEl.querySelector('.edit-card[data-tool-id="' + id + '"]');
      if (!el) {
        // Check for regular tool call
        el = messagesEl.querySelector('.tool-call[data-tool-id="' + id + '"]');
      }
      if (!el) return;
      
      const statusSpan = el.querySelector('.tool-call-status');
      if (!statusSpan) return;

      statusSpan.className = 'tool-call-status ' + status;
      if (status === 'success') {
        statusSpan.textContent = '✓';
      } else if (status === 'failed') {
        statusSpan.textContent = '✗';
      } else {
        statusSpan.textContent = '●';
      }
      
      checkScroll();
    }

    function addEditProposal(editId, filePath, filename) {
      // Close any open research group
      closeResearchGroup();
      
      const div = document.createElement('div');
      div.className = 'edit-card';
      div.dataset.editId = editId;
      div.dataset.filePath = filePath;

      const header = document.createElement('div');
      header.className = 'edit-card-header';
      
      header.innerHTML = '<span class="edit-card-icon">📝</span>' +
        '<span class="edit-card-filename" title="' + escapeHtml(filePath) + '">' + escapeHtml(filename) + '</span>' +
        '<span class="tool-call-status running">pending</span>';

      div.appendChild(header);
      messagesEl.appendChild(div);
      scrollToBottom(false);
      return div;
    }
    
    function updateEditProposal(editId, accepted) {
      const el = messagesEl.querySelector('.edit-card[data-edit-id="' + editId + '"]');
      if (!el) return;
      
      const statusSpan = el.querySelector('.tool-call-status');
      if (!statusSpan) return;

      if (accepted) {
        statusSpan.className = 'tool-call-status success';
        statusSpan.textContent = '✓ accepted';
        el.style.borderLeftColor = '#9ece6a';
      } else {
        statusSpan.className = 'tool-call-status failed';
        statusSpan.textContent = '✗ rejected';
        el.style.borderLeftColor = '#f7768e';
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
      streamingBuffer = '';
      currentResearchGroup = null;
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
                  streamingBuffer = '';
                }
                streamingBuffer += p.content;
                const body = currentAssistantEl.querySelector('.msg-body');
                if (body) {
                  body.textContent = streamingBuffer;
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
              // Final markdown render of complete response
              if (currentAssistantEl && streamingBuffer) {
                const body = currentAssistantEl.querySelector('.msg-body');
                if (body) {
                  body.innerHTML = renderMarkdown(streamingBuffer);
                }
                // Add narration styling
                currentAssistantEl.classList.add('narration');
                // Add KYREX label if not already present
                const existingLabel = currentAssistantEl.querySelector('.kyrex-label');
                if (!existingLabel) {
                  const label = document.createElement('div');
                  label.className = 'msg-label kyrex-label';
                  label.innerHTML = '<span class="kyrex-badge">KYREX</span>';
                  currentAssistantEl.insertBefore(label, currentAssistantEl.firstChild);
                }
              }
              currentAssistantEl = null;
              streamingBuffer = '';
              setGenerating(false);
              setEngineStatus('online');
              break;
            }
            case 'tool_start': {
              const toolId = p.id || 'tool_' + Date.now();
              const toolName = p.name || 'tool';
              const toolArgs = p.args || p.input || {};
              pendingToolCalls.push(toolId);
              addToolCall(toolName, toolId, toolArgs);
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
            case 'propose_edit': {
              // Show edit proposal in sidebar
              const editId = p.editId || 'edit_' + Date.now();
              const filePath = p.filePath || '';
              const filename = filePath.split(/[\\\\/]/).pop() || filePath;
              addEditProposal(editId, filePath, filename);
              break;
            }
            case 'edit_decision': {
              // Update edit proposal with decision
              const editId = p.editId;
              const accepted = p.accepted;
              updateEditProposal(editId, accepted);
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
          streamingBuffer = '';
          currentResearchGroup = null;
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