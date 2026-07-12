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

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct BridgeMessage {
    #[serde(flatten)]
    pub data: serde_json::Value,
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
