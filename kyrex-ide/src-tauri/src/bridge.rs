use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

pub struct EngineState {
    pub child: Mutex<Option<CommandChild>>,
}

impl Default for EngineState {
    fn default() -> Self {
        EngineState { child: Mutex::new(None) }
    }
}

/// Spawns the bundled kyrex-engine sidecar binary (resolved by Tauri based
/// on the platform target triple) and relays NDJSON stdout lines as
/// "bridge-message" events. Refuses to spawn if already running.
#[tauri::command]
pub async fn start_engine(app: AppHandle, state: State<'_, EngineState>, workspace_path: String) -> Result<(), String> {
    {
        let child_guard = state.child.lock().unwrap();
        if child_guard.is_some() {
            return Err("engine already running".into());
        }
    }

    let sidecar = app
        .shell()
        .sidecar("kyrex-engine")
        .map_err(|e| format!("failed to resolve sidecar: {e}"))?
        .env("KYREX_SURFACE", "Kyrex IDE")
        .env("KYREX_VSCODE", "1")
        .current_dir(&workspace_path);

    let (mut rx, child) = sidecar
        .spawn()
        .map_err(|e| format!("failed to spawn engine: {e}"))?;

    *state.child.lock().unwrap() = Some(child);

    let app_clone = app.clone();
    tokio::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    let line = String::from_utf8_lossy(&bytes).to_string();
                    let trimmed = line.trim();
                    if trimmed.is_empty() {
                        continue;
                    }
                    match serde_json::from_str::<serde_json::Value>(trimmed) {
                        Ok(val) => {
                            let _ = app_clone.emit("bridge-message", val);
                        }
                        Err(e) => {
                            let _ = app_clone.emit(
                                "bridge-error",
                                format!("failed to parse line: {e} | raw: {trimmed}"),
                            );
                        }
                    }
                }
                CommandEvent::Stderr(bytes) => {
                    let line = String::from_utf8_lossy(&bytes).to_string();
                    let _ = app_clone.emit("bridge-stderr", line);
                }
                CommandEvent::Terminated(_) => {
                    let _ = app_clone.emit("bridge-closed", ());
                    break;
                }
                _ => {}
            }
        }
    });

    Ok(())
}

/// Sends a single JSON payload to the engine's stdin, newline-terminated.
#[tauri::command]
pub async fn send_to_bridge(state: State<'_, EngineState>, payload: String) -> Result<(), String> {
    let mut guard = state.child.lock().unwrap();
    if let Some(child) = guard.as_mut() {
        let line = format!("{}\n", payload.trim());
        child
            .write(line.as_bytes())
            .map_err(|e| format!("failed to write to engine stdin: {e}"))
    } else {
        Err("engine not started".into())
    }
}

#[tauri::command]
pub async fn stop_engine(state: State<'_, EngineState>) -> Result<(), String> {
    let mut guard = state.child.lock().unwrap();
    if let Some(child) = guard.take() {
        let _ = child.kill();
    }
    Ok(())
}

#[derive(Serialize, Debug, Clone)]
pub struct FileEntry {
    pub name: String,
    pub path: String,
    pub is_dir: bool,
}

#[tauri::command]
pub async fn list_dir(path: String) -> Result<Vec<FileEntry>, String> {
    const SKIP: &[&str] = &[
        "node_modules", ".git", "target", "dist", "build",
        "__pycache__", ".vscode", "venv", "build_venv",
    ];

    let mut entries = tokio::fs::read_dir(&path)
        .await
        .map_err(|e| format!("failed to read dir: {e}"))?;

    let mut result = Vec::new();
    while let Some(entry) = entries
        .next_entry()
        .await
        .map_err(|e| format!("failed to read entry: {e}"))?
    {
        let name = entry.file_name().to_string_lossy().to_string();
        if SKIP.contains(&name.as_str()) {
            continue;
        }
        let file_type = entry
            .file_type()
            .await
            .map_err(|e| format!("failed to get file type: {e}"))?;
        result.push(FileEntry {
            name,
            path: entry.path().to_string_lossy().to_string(),
            is_dir: file_type.is_dir(),
        });
    }

    result.sort_by(|a, b| match (a.is_dir, b.is_dir) {
        (true, false) => std::cmp::Ordering::Less,
        (false, true) => std::cmp::Ordering::Greater,
        _ => a.name.to_lowercase().cmp(&b.name.to_lowercase()),
    });

    Ok(result)
}

#[tauri::command]
pub async fn read_file_contents(path: String) -> Result<String, String> {
    match tokio::fs::read_to_string(&path).await {
        Ok(contents) => Ok(contents),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(e) => Err(format!("failed to read file: {e}")),
    }
}

#[tauri::command]
pub async fn write_file_contents(path: String, contents: String) -> Result<(), String> {
    tokio::fs::write(&path, &contents)
        .await
        .map_err(|e| format!("failed to write file: {e}"))
}

fn config_path(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("failed to resolve app data dir: {e}"))?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("failed to create config dir: {e}"))?;
    Ok(dir.join("workspace_config.json"))
}

#[derive(Serialize, Deserialize)]
pub struct WorkspaceConfig {
    pub path: String,
}

#[derive(Serialize, Deserialize)]
pub struct SessionConfig {
    pub name: String,
}

fn session_config_path(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("failed to resolve app data dir: {e}"))?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("failed to create config dir: {e}"))?;
    Ok(dir.join("session_config.json"))
}

/// Saves the workspace path to a persisted config file.
#[tauri::command]
pub async fn save_workspace_config(app: AppHandle, path: String) -> Result<(), String> {
    let cfg_path = config_path(&app)?;
    let config = WorkspaceConfig { path };
    let json = serde_json::to_string(&config).map_err(|e| format!("serialization error: {e}"))?;
    tokio::fs::write(&cfg_path, json)
        .await
        .map_err(|e| format!("failed to write config: {e}"))?;
    Ok(())
}

/// Loads the persisted workspace path, or returns null if none saved.
#[tauri::command]
pub async fn load_workspace_config(app: AppHandle) -> Result<Option<String>, String> {
    let cfg_path = config_path(&app)?;
    if !cfg_path.exists() {
        return Ok(None);
    }
    let json = tokio::fs::read_to_string(&cfg_path)
        .await
        .map_err(|e| format!("failed to read config: {e}"))?;
    let config: WorkspaceConfig =
        serde_json::from_str(&json).map_err(|e| format!("parse error: {e}"))?;
    Ok(Some(config.path))
}

/// Runs the bundled engine binary in --wizard-step mode: a short-lived,
/// one-shot process (not the long-running chat sidecar) that tests a
/// provider connection or fetches available models, then exits.
/// See kyrex_engine/core_bridge.py's _run_wizard_step() for the Python side.
#[tauri::command]
pub async fn run_wizard_step(app: AppHandle, request_json: String) -> Result<String, String> {
    let sidecar = app
        .shell()
        .sidecar("kyrex-engine")
        .map_err(|e| format!("failed to resolve sidecar: {e}"))?
        .args(["--wizard-step"]);

    let (mut rx, mut child) = sidecar
        .spawn()
        .map_err(|e| format!("failed to spawn wizard-step process: {e}"))?;

    let line = format!("{}\n", request_json.trim());
    child
        .write(line.as_bytes())
        .map_err(|e| format!("failed to write to wizard-step stdin: {e}"))?;

    let mut output = String::new();
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                output.push_str(&String::from_utf8_lossy(&bytes));
            }
            CommandEvent::Terminated(_) => break,
            _ => {}
        }
    }

    let result_line = output
        .lines()
        .find(|l| !l.trim().is_empty())
        .ok_or("wizard-step produced no output")?;

    Ok(result_line.to_string())
}

/// Lists available Kyrex sessions for a workspace by reading
/// {workspace}/.px_sessions/*.json filenames (branch names).
/// Returns just the names (without .json), sorted alphabetically,
/// with "main" always first if present.
#[tauri::command]
pub async fn list_sessions(workspace_path: String) -> Result<Vec<String>, String> {
    let sessions_dir = std::path::Path::new(&workspace_path).join(".px_sessions");

    let mut entries = match tokio::fs::read_dir(&sessions_dir).await {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => return Err(format!("failed to read sessions dir: {e}")),
    };

    let mut names = Vec::new();
    while let Some(entry) = entries
        .next_entry()
        .await
        .map_err(|e| format!("failed to read entry: {e}"))?
    {
        let file_name = entry.file_name().to_string_lossy().to_string();
        if let Some(stripped) = file_name.strip_suffix(".json") {
            names.push(stripped.to_string());
        }
    }

    names.sort();
    // "main" always first if present
    if let Some(pos) = names.iter().position(|n| n == "main") {
        let main = names.remove(pos);
        names.insert(0, main);
    }

    Ok(names)
}

#[tauri::command]
pub async fn save_session_config(app: AppHandle, name: String) -> Result<(), String> {
    let cfg_path = session_config_path(&app)?;
    let config = SessionConfig { name };
    let json = serde_json::to_string(&config).map_err(|e| format!("serialization error: {e}"))?;
    tokio::fs::write(&cfg_path, json)
        .await
        .map_err(|e| format!("failed to write session config: {e}"))?;
    Ok(())
}

#[tauri::command]
pub async fn load_session_config(app: AppHandle) -> Result<Option<String>, String> {
    let cfg_path = session_config_path(&app)?;
    if !cfg_path.exists() {
        return Ok(None);
    }
    let json = tokio::fs::read_to_string(&cfg_path)
        .await
        .map_err(|e| format!("failed to read session config: {e}"))?;
    let config: SessionConfig =
        serde_json::from_str(&json).map_err(|e| format!("parse error: {e}"))?;
    Ok(Some(config.name))
}

// ═══════════════════════════════════════════════════════════════════
// Race Mode
// ═══════════════════════════════════════════════════════════════════
//
// Race mode spawns N parallel engine subprocesses, one per model, each
// working in its own cloned copy of the workspace. Mirrors the TUI's
// internal/race package: confirm_request messages are auto-approved
// (lanes are disposable clones, not the real workspace), and the real
// safety gate happens later, at merge time.

use std::collections::HashMap;

#[derive(Clone, Serialize)]
pub struct RaceLaneInfo {
    pub id: u32,
    pub model: String,
    pub dir: String,
}

#[derive(Clone, Serialize)]
pub struct MergeResult {
    pub files_changed: u32,
}

pub struct RaceLane {
    pub child: std::sync::Arc<Mutex<CommandChild>>,
}

pub struct RaceState {
    pub lanes: Mutex<HashMap<u32, RaceLane>>,
    pub race_dir: Mutex<Option<String>>,
}

impl Default for RaceState {
    fn default() -> Self {
        RaceState {
            lanes: Mutex::new(HashMap::new()),
            race_dir: Mutex::new(None),
        }
    }
}

const RACE_CLONE_EXCLUDES: &[&str] = &[
    ".venv", "venv", "build_venv", "node_modules",
    "__pycache__", ".rifts", "dist", "build", "target",
    ".px_history",
];

/// Clones a workspace directory for one race lane, mirroring the TUI's
/// cloneDir(): prefers `rsync -a --exclude ...`, falls back to
/// `cp -a --reflink=auto` if rsync isn't available (excludes won't apply
/// in the fallback path, matching upstream behavior exactly).
fn clone_race_lane_dir(src: &str, dst: &str) -> Result<(), String> {
    std::fs::create_dir_all(dst).map_err(|e| format!("mkdir lane dir: {e}"))?;

    let rsync_available = std::process::Command::new("which")
        .arg("rsync")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);

    if rsync_available {
        let mut args: Vec<String> = vec!["-a".to_string()];
        for excl in RACE_CLONE_EXCLUDES {
            args.push("--exclude".to_string());
            args.push(excl.to_string());
        }
        args.push(format!("{}/", src));
        args.push(format!("{}/", dst));

        let output = std::process::Command::new("rsync")
            .args(&args)
            .output()
            .map_err(|e| format!("failed to run rsync: {e}"))?;

        if !output.status.success() {
            return Err(format!(
                "rsync failed: {}",
                String::from_utf8_lossy(&output.stderr)
            ));
        }
        Ok(())
    } else {
        let output = std::process::Command::new("cp")
            .args(["-a", "--reflink=auto", &format!("{}/.", src), &format!("{}/", dst)])
            .output()
            .map_err(|e| format!("failed to run cp: {e}"))?;

        if !output.status.success() {
            return Err(format!(
                "cp failed: {}",
                String::from_utf8_lossy(&output.stderr)
            ));
        }
        Ok(())
    }
}

/// Starts a race: clones the workspace once per model, spawns a sidecar
/// engine instance in each clone, and relays each lane's NDJSON stdout
/// as "race-lane-message" events tagged with laneId. confirm_request
/// messages are auto-approved (written back to that lane's stdin)
/// exactly as the TUI does, since lanes are disposable clones — the
/// real safety gate happens later, at merge time.
#[tauri::command]
pub async fn start_race(
    app: AppHandle,
    race_state: State<'_, RaceState>,
    task: String,
    models: Vec<String>,
    workspace_path: String,
) -> Result<Vec<RaceLaneInfo>, String> {
    {
        let lanes = race_state.lanes.lock().unwrap();
        if !lanes.is_empty() {
            return Err("a race is already running".into());
        }
    }

    let race_dir = std::env::temp_dir()
        .join("kyrex-races")
        .join(format!("race-{}", std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()));
    std::fs::create_dir_all(&race_dir).map_err(|e| format!("failed to create race dir: {e}"))?;
    *race_state.race_dir.lock().unwrap() = Some(race_dir.to_string_lossy().to_string());

    let mut lane_infos = Vec::new();

    for (i, model) in models.iter().enumerate() {
        let lane_dir = race_dir.join(format!("lane-{}", i));
        clone_race_lane_dir(&workspace_path, &lane_dir.to_string_lossy())?;

        // Write .kx-lane marker, matching TUI behavior
        let marker = format!("lane={}\nmodel={}\nrace={}\n", i, model, race_dir.to_string_lossy());
        std::fs::write(lane_dir.join(".kx-lane"), marker)
            .map_err(|e| format!("failed to write .kx-lane marker: {e}"))?;

        write_lane_config(&lane_dir, model)?;

        let sidecar = app
            .shell()
            .sidecar("kyrex-engine")
            .map_err(|e| format!("failed to resolve sidecar: {e}"))?
            .current_dir(&lane_dir)
            .env("KYREX_SURFACE", "Kyrex IDE")
            .env("KYREX_VSCODE", "1")
            .env("WORKSPACE_ROOT", lane_dir.to_string_lossy().to_string())
            .env("PROJECT_SOURCE_ROOT", &workspace_path);

        let (mut rx, child) = sidecar
            .spawn()
            .map_err(|e| format!("failed to spawn lane {i}: {e}"))?;
        let child = std::sync::Arc::new(Mutex::new(child));

        // Send the initial task immediately (matches TUI: deferred send
        // is really just "send once we see the process is up," which in
        // practice happens fast enough to send right after spawn).
        {
            let payload = serde_json::json!({ "type": "chat", "content": task.clone() });
            let line = format!("{}\n", payload.to_string());
            child
                .lock()
                .unwrap()
                .write(line.as_bytes())
                .map_err(|e| format!("failed to send task to lane {i}: {e}"))?;
        }

        let lane_id = i as u32;
        let app_clone = app.clone();
        let child_for_reader = child.clone();
        tokio::spawn(async move {
            while let Some(event) = rx.recv().await {
                match event {
                    CommandEvent::Stdout(bytes) => {
                        let line = String::from_utf8_lossy(&bytes).to_string();
                        let trimmed = line.trim();
                        if trimmed.is_empty() {
                            continue;
                        }
                        match serde_json::from_str::<serde_json::Value>(trimmed) {
                            Ok(val) => {
                                // Auto-approve confirm_request, matching TUI's
                                // race-mode behavior (lanes are disposable).
                                if val.get("type").and_then(|t| t.as_str()) == Some("confirm_request") {
                                    if let Some(id) = val.get("id").and_then(|i| i.as_str()) {
                                        let response = serde_json::json!({
                                            "type": "confirm_response",
                                            "id": id,
                                            "approved": true
                                        });
                                        let resp_line = format!("{}\n", response.to_string());
                                        if let Ok(mut c) = child_for_reader.lock() {
                                            let _ = c.write(resp_line.as_bytes());
                                        }
                                    }
                                }
                                let msg_type_2 = val.get("type").and_then(|t| t.as_str());
                                if msg_type_2 == Some("propose_edit") {
                                    if let Some(edit_id) = val.get("editId").and_then(|i| i.as_str()) {
                                        let response2 = serde_json::json!({
                                            "type": "edit_decision",
                                            "editId": edit_id,
                                            "accepted": true
                                        });
                                        let resp_line2 = format!("{}\n", response2.to_string());
                                        if let Ok(mut c) = child_for_reader.lock() {
                                            let _ = c.write(resp_line2.as_bytes());
                                        }
                                    }
                                }
                                let _ = app_clone.emit(
                                    "race-lane-message",
                                    serde_json::json!({ "laneId": lane_id, "event": val }),
                                );
                            }
                            Err(e) => {
                                let _ = app_clone.emit(
                                    "race-lane-error",
                                    serde_json::json!({ "laneId": lane_id, "error": format!("{e}: {trimmed}") }),
                                );
                            }
                        }
                    }
                    CommandEvent::Terminated(_) => {
                        let _ = app_clone.emit(
                            "race-lane-closed",
                            serde_json::json!({ "laneId": lane_id }),
                        );
                        break;
                    }
                    _ => {}
                }
            }
        });

        lane_infos.push(RaceLaneInfo {
            id: lane_id,
            model: model.clone(),
            dir: lane_dir.to_string_lossy().to_string(),
        });

        race_state.lanes.lock().unwrap().insert(
            lane_id,
            RaceLane {
                child,
            },
        );
    }

    Ok(lane_infos)
}

/// Reads the real ~/.px/config.json (provider, api_key, base_url), swaps
/// in the given model, and writes the result into the lane clone's own
/// .px/config.json — so each lane genuinely uses its assigned model,
/// not silently falling back to whatever the real workspace/home config
/// has. Mirrors the TUI's setModelInConfig step in cloneLane().
fn write_lane_config(lane_dir: &std::path::Path, model: &str) -> Result<(), String> {
    let home_config_path = dirs_next_home()
        .ok_or("could not resolve home directory")?
        .join(".px")
        .join("config.json");

    let raw = std::fs::read_to_string(&home_config_path)
        .map_err(|e| format!("failed to read {}: {e}", home_config_path.display()))?;

    let mut config: serde_json::Value = serde_json::from_str(&raw)
        .map_err(|e| format!("failed to parse home config: {e}"))?;

    config["model"] = serde_json::Value::String(model.to_string());

    let lane_px_dir = lane_dir.join(".px");
    std::fs::create_dir_all(&lane_px_dir)
        .map_err(|e| format!("failed to create lane .px dir: {e}"))?;

    let out = serde_json::to_string_pretty(&config)
        .map_err(|e| format!("failed to serialize lane config: {e}"))?;

    std::fs::write(lane_px_dir.join("config.json"), out)
        .map_err(|e| format!("failed to write lane config: {e}"))?;

    Ok(())
}

/// Minimal home-dir resolution without pulling in a new crate dependency.
fn dirs_next_home() -> Option<std::path::PathBuf> {
    std::env::var_os("HOME").map(std::path::PathBuf::from)
}

/// Computes a unified diff between the original workspace and a race
/// lane's clone directory, mirroring the TUI's DiffLane(). Uses `diff
/// -ruN`, excluding the same directories as clone (plus .kx-lane and
/// .px, which are lane-only metadata, not real project changes).
#[tauri::command]
pub async fn diff_race_lane(workspace_path: String, lane_dir: String) -> Result<String, String> {
    let mut args: Vec<String> = vec!["-ruN".to_string()];
    for excl in RACE_CLONE_EXCLUDES {
        args.push("-x".to_string());
        args.push(excl.to_string());
    }
    args.push("-x".to_string());
    args.push(".kx-lane".to_string());
    args.push("-x".to_string());
    args.push(".px".to_string());
    args.push("-x".to_string());
    args.push(".px_sessions".to_string());
    args.push("-x".to_string());
    args.push(".px_history".to_string());
    args.push(workspace_path.clone());
    args.push(lane_dir.clone());

    let output = std::process::Command::new("diff")
        .args(&args)
        .output()
        .map_err(|e| format!("failed to run diff: {e}"))?;

    // diff exits 0 (no differences), 1 (differences found), or 2 (error).
    // Both 0 and 1 are "success" for our purposes; only 2 is a real error.
    match output.status.code() {
        Some(0) | Some(1) => Ok(String::from_utf8_lossy(&output.stdout).to_string()),
        _ => Err(format!(
            "diff failed: {}",
            String::from_utf8_lossy(&output.stderr)
        )),
    }
}

/// Merges a race lane\'s changes back into the real workspace. Uses `diff
/// -rqN` (brief, recursive) to find files that differ, files only in the
/// lane (added), and files only in the workspace (deleted). Then:
///   - Modified/added files: copies from lane_dir to workspace_path,
///     creating parent directories as needed.
///   - Deleted files (exist in workspace but not in lane): removes from
///     workspace_path.
/// Returns a summary of files changed. Mirror of the TUI\'s MergeBack.
#[tauri::command]
pub async fn merge_race_lane(workspace_path: String, lane_dir: String) -> Result<MergeResult, String> {
    let mut args: Vec<String> = vec!["-rqN".to_string()];
    for excl in RACE_CLONE_EXCLUDES {
        args.push("-x".to_string());
        args.push(excl.to_string());
    }
    args.push("-x".to_string());
    args.push(".kx-lane".to_string());
    args.push("-x".to_string());
    args.push(".px".to_string());
    args.push("-x".to_string());
    args.push(".px_sessions".to_string());
    args.push("-x".to_string());
    args.push(".px_history".to_string());
    args.push(workspace_path.clone());
    args.push(lane_dir.clone());

    let output = std::process::Command::new("diff")
        .args(&args)
        .output()
        .map_err(|e| format!("failed to run diff: {e}"))?;

    // Exit code 0 = identical, 1 = differences, 2 = error.
    match output.status.code() {
        Some(0) => return Ok(MergeResult { files_changed: 0 }),
        Some(1) => {} // differences found — proceed
        _ => return Err(format!("diff failed: {}", String::from_utf8_lossy(&output.stderr))),
    }

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let mut files_changed: u32 = 0;

    for line in stdout.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }

        // Pattern: "Only in /path: filename"
        if let Some(rest) = line.strip_prefix("Only in ") {
            if let Some(colon_pos) = rest.rfind(": ") {
                let dir_part = &rest[..colon_pos];
                let file_name = &rest[colon_pos + 2..];
                let ws_prefix = workspace_path.trim_end_matches('/');
                let lane_prefix = lane_dir.trim_end_matches('/');

                if dir_part.strip_prefix(ws_prefix).map(|s| s.is_empty() || s.starts_with('/')).unwrap_or(false)
                    && dir_part != lane_prefix
                {
                    // File only in workspace → deleted in lane. Remove from workspace.
                    let rel = dir_part.strip_prefix(ws_prefix).unwrap().trim_start_matches('/');
                    let rel_path = if rel.is_empty() { file_name.to_string() } else { format!("{}/{}", rel, file_name) };
                    let target = std::path::Path::new(&workspace_path).join(&rel_path);
                    if target.exists() {
                        let _ = std::fs::remove_file(&target);
                        if let Some(parent) = target.parent() {
                            let _ = std::fs::remove_dir(parent);
                        }
                        files_changed += 1;
                    }
                } else if dir_part.strip_prefix(lane_prefix).map(|s| s.is_empty() || s.starts_with('/')).unwrap_or(false)
                    && dir_part != ws_prefix
                {
                    // File only in lane → added in lane. Copy to workspace.
                    let rel = dir_part.strip_prefix(lane_prefix).unwrap().trim_start_matches('/');
                    let rel_path = if rel.is_empty() { file_name.to_string() } else { format!("{}/{}", rel, file_name) };
                    let src = std::path::Path::new(&lane_dir).join(&rel_path);
                    let dst = std::path::Path::new(&workspace_path).join(&rel_path);
                    if src.exists() {
                        if let Some(parent) = dst.parent() {
                            std::fs::create_dir_all(parent)
                                .map_err(|e| format!("failed to create dir {}: {e}", parent.display()))?;
                        }
                        std::fs::copy(&src, &dst)
                            .map_err(|e| format!("failed to copy {} -> {}: {e}", src.display(), dst.display()))?;
                        files_changed += 1;
                    }
                }
            }
            continue;
        }

        // Pattern: "Files A and B differ"
        if line.starts_with("Files ") && line.contains(" and ") && line.ends_with(" differ") {
            let middle = &line["Files ".len()..line.len() - " differ".len()];
            if let Some(and_pos) = middle.find(" and ") {
                let a_path = middle[..and_pos].trim();
                let b_path = middle[and_pos + 5..].trim();

                // b is the lane_dir side in our diff args order,
                // a is the workspace_path side.
                let lane_path = std::path::Path::new(b_path);
                let ws_path = std::path::Path::new(a_path);

                if lane_path.exists() {
                    if let Some(parent) = ws_path.parent() {
                        std::fs::create_dir_all(parent)
                            .map_err(|e| format!("failed to create dir {}: {e}", parent.display()))?;
                    }
                    std::fs::copy(lane_path, ws_path)
                        .map_err(|e| format!("failed to copy {} -> {}: {e}", lane_path.display(), ws_path.display()))?;
                    files_changed += 1;
                }
            }
            continue;
        }
    }

    Ok(MergeResult { files_changed })
}

/// Kills all running lane child processes and clears RaceState. Used for
/// cleanup after merge or discard — returns the UI to normal chat view.
#[tauri::command]
pub async fn kill_race(race_state: State<'_, RaceState>) -> Result<(), String> {
    let mut lanes = race_state.lanes.lock().unwrap();
    for (_, lane) in lanes.drain() {
        if let Ok(mutex) = std::sync::Arc::try_unwrap(lane.child) {
            if let Ok(child) = mutex.into_inner() {
                let _ = child.kill();
            }
        }
    }
    *race_state.race_dir.lock().unwrap() = None;
    Ok(())
}
