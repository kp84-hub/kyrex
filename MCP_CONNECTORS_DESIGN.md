# Kyrex TUI Curated MCP Connectors — Design Plan

**Status:** Planning only

**Scope:** Design for a browsable curated MCP connector directory in the Kyrex TUI. This document does not implement the feature and does not change runtime behavior.

## 1. Verified current architecture

The current MCP path was inspected directly:

- `kyrex_engine/kyrex/tools/mcp.py` defines `MCPServer` and `MCPManager`.
- `MCPServer.start()` launches `[command] + args` with `subprocess.Popen`, performs MCP `initialize`, then `tools/list`, and stores the discovered tools.
- `MCPManager` loads persisted servers from `~/.kyrex/mcp_servers.json`, starts them through `start_all()`, and saves additions/removals to the same file.
- `core.py` constructs `self.mcp = MCPManager()` during engine initialization and calls `self.mcp.start_all()` at startup.
- `core.py` extends the model tool schemas with `self.mcp.get_tool_schemas()` and routes `mcp_...` tool calls back through `self.mcp.call_tool`.
- The existing slash-command implementation handles:
  - `/mcp` — lists configured server names;
  - `/mcp add <name> <command> [args...]` — creates and persists an `MCPServer` configuration;
  - `/mcp remove <name>` — stops, removes, and persists the server configuration.

The proposed directory must therefore be a discovery and command-construction layer. It should not replace `MCPManager`, invent a second persistence format for active servers, or implement a second MCP process lifecycle.

## 2. Existing TUI patterns to reuse

### 2.1 Slash-command picker

The TUI implementation is in `tui/update_keys.go` and `tui/view.go`:

- `availableCommands` is the source list.
- Typing `/` into an empty textarea calls `activateCommandPicker("")`.
- Typing after `/` filters the list by prefix.
- `handleCommandPickerKey` owns the active overlay and intercepts Up, Down, Enter/Tab, Escape, Backspace, and runes.
- `selectCommandPickerItem` fills the selected command into the textarea and closes the picker.
- `RenderCommandPicker` renders the popup above the input, with a highlighted row and keyboard hints.
- Existing tests in `tui/update_keys_test.go` cover activation, filtering, selection, and cancellation.

This is the correct entry-point pattern for the new feature: add `/mcp browse` as a command-completion target and open the connector picker after that command is selected or submitted. Do not create a new global key binding or a separate command-discovery UI.

### 2.2 Model picker

The model picker is a more appropriate overlay model for the connector directory:

- The engine emits a `tui_pause` event with `Value == "model_picker"` and a list of model names.
- `tui/update_engine.go` receives it in `handlePause`, populates picker state, and marks `_modelPickerActive`.
- `tui/update_keys.go` intercepts keys while active in `handleModelPickerKey`.
- Up/Down wraps through items; typed text filters; Enter selects; Escape cancels.
- On selection, it sends a command through `SendFunc`, records the command in history, sets a toast, and closes the overlay.
- `tui/view.go` renders a titled list, current/selected state, filter information, and keyboard instructions.

The connector picker should reuse this lifecycle and interaction contract. The likely implementation shape is a connector-specific `tui_pause` payload and a connector-specific picker state, but the exact list/filter/select/cancel behavior should remain parallel to the existing model picker rather than introducing a new Bubble Tea component or modal convention.

The picker should be opened by the engine in response to `/mcp browse`, using the same stdout JSON event channel already used for `tui_pause` model selection. The Go TUI should not need to know how MCP processes are started.

## 3. Proposed user flow

### 3.1 Trigger

1. User types `/` and selects `/mcp` through the existing command picker.
2. User enters `browse` as the subcommand, producing `/mcp browse`.
3. The engine recognizes `/mcp browse` and emits a new pause event, for example:

   ```text
   type: tui_pause
   value: mcp_connector_picker
   files: [connector records]
   ```

4. The TUI opens the connector picker using the same interception and rendering pattern as the model picker.

The existing non-TUI behavior should remain useful: if `/mcp browse` is invoked where an interactive picker is unavailable, the engine should print a compact list and explain that the command is intended for the TUI.

### 3.2 What the picker shows

Each row should show:

- connector display name;
- short capability description;
- transport/launch family, such as `npx`, `uvx`, or local executable;
- auth label: `none`, `environment variable`, `browser sign-in`, or `manual setup`;
- verification state/version date if available.

The detail area for the highlighted row should show:

- the exact command and arguments that will be passed to `/mcp add`;
- required environment variables, without displaying secret values;
- filesystem scope or other permission scope;
- whether a browser may open;
- whether the connector is marked `needs verification`.

The default list should contain only entries with a usable command recipe. Entries marked `needs verification` may be shown in a separate section but must not be selectable for installation until verified, unless the user explicitly chooses a manual command path.

### 3.3 Selection behavior

On Enter:

1. Show a confirmation/detail step if the connector has filesystem access, credentials, browser auth, or a command that will download a package.
2. On confirmation, construct the exact existing command:

   ```text
   /mcp add <configured-name> <command> [args...]
   ```

3. Send that command through the same `SendFunc` mechanism used by the model picker.
4. Record the command in history and show a toast such as `MCP connector added: Filesystem`.
5. Let the existing engine-side `/mcp add` handler persist it to `~/.kyrex/mcp_servers.json`.
6. Do not directly mutate `MCPManager` from Go and do not duplicate persistence in the TUI.

The first version should use a deterministic configured-name slug from the manifest, with collision handling such as `filesystem-2`. If the name is already configured, the picker should offer `replace`, `add with another name`, or `cancel`; it should not silently overwrite an existing server.

The command is persisted immediately by the existing `/mcp add` path. The server will be started by the normal MCP startup lifecycle; if hot-start behavior is later desired, it should be added to `MCPManager` deliberately rather than hidden in the picker plan.

On Escape, close the picker without sending a command or changing the active server set.

## 4. Curated directory contents

The initial directory should be small, useful, and conservative. A connector record should include at least:

- stable ID and display name;
- description and category;
- command plus argument template;
- environment-variable requirements;
- local path/argument requirements;
- auth mode and user-facing warning;
- source documentation URL;
- verification status, package version, and last verification date.

### 4.1 Candidates with recognizable documented launch recipes

These are candidate records based on known MCP documentation/package identifiers. The registry check attempted during planning timed out, so these must still be revalidated against the current upstream README and package registry before being shipped as selectable records.

| ID | Candidate command | Requirements | Status for curated list |
|---|---|---|---|
| `filesystem` | `npx -y @modelcontextprotocol/server-filesystem <allowed-directory>` | Node/npm; one or more explicitly allowed directories | **Candidate; verify current package/release** |
| `github` | `npx -y @modelcontextprotocol/server-github` | `GITHUB_PERSONAL_ACCESS_TOKEN` | **Candidate; verify current package/release and token name** |
| `postgres` | `npx -y @modelcontextprotocol/server-postgres <postgresql-connection-string>` | Node/npm; database connection string | **Candidate; verify current package/release and argument syntax** |
| `sqlite` | `npx -y @modelcontextprotocol/server-sqlite --db-path <database-file>` | Node/npm; database file path | **Candidate; verify current package/release and flags** |
| `puppeteer` | `npx -y @modelcontextprotocol/server-puppeteer` | Node/npm; local browser runtime may be downloaded/used | **Candidate; verify current package/release and browser behavior** |
| `brave-search` | `npx -y @modelcontextprotocol/server-brave-search` | `BRAVE_API_KEY` | **Candidate; verify current package/release and token name** |

These commands are examples of real identifiers known from MCP server documentation, not commands invented for this design. However, because the live registry probe was unavailable, the implementation must not promote them to an unqualified “verified” catalog without a release-time verification pass.

### 4.2 OAuth/browser-auth candidates

These need especially careful treatment:

| ID | Candidate | Status |
|---|---|---|
| `notion` | `npx -y @notionhq/notion-mcp-server` | **Needs verification before shipping.** Confirm the current package, required token/configuration, and whether the current supported path is local package launch or Notion's hosted MCP endpoint. |
| `google-drive` | A Google Drive MCP server exists in the ecosystem, but an exact currently-supported package/command was not confirmed here. | **Needs verification; do not guess a command.** |
| `gmail` | Gmail MCP integrations exist, but an exact currently-supported package/command and auth contract were not confirmed here. | **Needs verification; do not guess a command.** |
| `slack` | Slack MCP integrations exist, but the exact current official/community package and command were not confirmed here. | **Needs verification; do not guess a command.** |

The catalog may include these as informational “coming soon / needs verification” entries, but they must not be selectable into `/mcp add` until their exact upstream launch instructions are verified and recorded.

### 4.3 What not to do

- Do not publish a guessed `uvx`, `npx`, Docker, or Python command for Google Drive, Gmail, Notion, or Slack.
- Do not imply that a package is official merely because its name contains `modelcontextprotocol`.
- Do not hide API keys in the manifest or generated command.
- Do not present a connector as verified without a package/source URL and verification date.

## 5. OAuth and browser authentication UX

Kyrex should not claim to implement OAuth. Authentication belongs to the selected MCP server, exactly as its upstream documentation defines it.

For a connector whose server opens a browser or starts an OAuth flow, the picker detail/confirmation text should say plainly:

> This connector requires authentication handled by the MCP server. Selecting it may open a browser so you can sign in and grant access. Kyrex does not receive or manage your OAuth credentials.

The picker should additionally show:

- the service and requested scope, if known;
- whether a browser will open during startup or first tool use;
- any required environment variable or token setup;
- a link or copyable upstream setup reference;
- a warning that the server process will have the resulting access granted by the user.

For Google Drive, Gmail, and Notion specifically, the initial curated records should remain `needs verification` until the server's current auth flow is confirmed. Once confirmed, the record should describe the browser flow honestly, for example: “The selected MCP server handles OAuth and may open your browser to sign in.” It should never say “Kyrex connected Google Drive” unless Kyrex itself actually owns that flow, which this plan does not propose.

## 6. Where the curated list should live

### Recommended source of truth: versioned JSON manifest

Store the bundled fallback manifest in a dedicated data file, for example:

```text
assets/mcp-connectors.json
```

The manifest should be data-only and schema-versioned. A record should look conceptually like:

```json
{
  "schema_version": 1,
  "connectors": [
    {
      "id": "filesystem",
      "name": "Filesystem",
      "description": "Read and write explicitly allowed local directories.",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "<directory>"],
      "requirements": ["Node.js/npm", "one allowed directory"],
      "auth": {"mode": "none", "warning": "The server can access the directory you provide."},
      "source_url": "...",
      "verification": {"status": "needs_verification", "checked_at": "..."}
    }
  ]
}
```

Do not use a Go slice as the only source of truth: it makes catalog review and updates less convenient and couples content changes to UI code. Do not place the catalog in the Python MCP manager: the manager should remain responsible for configured active servers, not product discovery metadata.

### Updatability without a rebuild

Use a two-layer lookup:

1. bundled manifest, always available offline and shipped with Kyrex;
2. optional user override/update manifest under `~/.kyrex/mcp-connectors.json`.

A future update command or startup refresh can download a signed/versioned manifest to the user path. The updater should use atomic replacement, retain the last known-good file, enforce schema validation, and never execute anything merely because it appears in a downloaded catalog.

For safety and reproducibility:

- default to the bundled manifest if the remote update is absent, invalid, stale, or unavailable;
- display the manifest source and verification timestamp;
- require a user confirmation before installing a command from a remotely updated entry;
- treat command, arguments, environment requirements, and auth metadata as untrusted display/configuration data until the user confirms;
- provide a command such as `/mcp catalog refresh` only after the update mechanism has a defined trust/signature policy.

The override may add or revise entries, but it should not silently alter already-configured servers in `~/.kyrex/mcp_servers.json`.

## 7. Engine/TUI boundary

The engine should own:

- loading and validating the catalog;
- applying built-in defaults and user override precedence;
- resolving placeholders such as directory paths or connection strings;
- emitting connector records to the TUI;
- accepting the selected connector as a normal `/mcp add` command;
- retaining the existing `MCPManager` lifecycle and persistence behavior.

The TUI should own:

- the picker overlay state;
- filtering and keyboard navigation;
- rendering descriptions, requirements, warnings, and verification status;
- confirmation presentation;
- sending the selected `/mcp add` command through `SendFunc`.

The TUI should not spawn `npx`, `uvx`, Docker, Python, or MCP processes. It should not handle OAuth tokens or write MCP configuration files directly.

## 8. Safety and validation requirements

Before a connector becomes selectable:

1. Validate the manifest schema.
2. Validate that the command is a non-empty executable identifier and args are an array of strings.
3. Validate placeholder requirements and prompt for values rather than interpolating shell syntax.
4. Pass command and args as structured fields through the existing command path; avoid constructing a shell command string that is later shell-parsed.
5. Show network, filesystem, credential, and browser warnings before confirmation.
6. Preserve the exact command/args in the persisted MCP configuration so `/mcp remove` and startup behavior remain predictable.
7. Record startup failures through the existing MCP status/error output; the picker should not claim that a server is healthy merely because it was added.

A later improvement could add an explicit `enabled`/status view, but that is separate from the curated-directory phase.

## 9. Verification plan before implementation

Because the registry lookup was unavailable during this planning pass, implementation should begin with a catalog verification checklist:

- Open each candidate's current upstream documentation.
- Confirm the exact package identifier, command, argument order, environment variable names, and supported Node/Python runtime.
- Confirm whether the package still speaks the MCP stdio transport expected by `MCPServer`.
- Run each non-OAuth candidate in a disposable environment and verify `initialize` plus `tools/list`.
- For OAuth candidates, verify the actual browser/auth behavior and document it in the manifest.
- Mark any candidate that cannot be confirmed as `needs_verification`; do not guess.

## 10. Recommended delivery phases

1. **Catalog schema and bundled manifest:** add the data model, validation, and a small verified set.
2. **`/mcp browse` engine event:** expose catalog records through the existing `tui_pause` channel.
3. **TUI picker:** reuse model-picker state/keyboard/rendering conventions and add tests parallel to `update_keys_test.go`.
4. **Selection-to-`/mcp add` flow:** add detail/confirmation, placeholder prompting, collision handling, history, and toast behavior.
5. **User override manifest:** load `~/.kyrex/mcp-connectors.json` with validation and bundled fallback.
6. **Refresh/update policy:** implement only after signing, trust, rollback, and offline behavior are specified.

This sequence keeps the existing MCP runtime untouched while adding the missing browsable discovery layer.
