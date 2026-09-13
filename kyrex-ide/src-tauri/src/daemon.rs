//! daemon.rs — background engine discovery, spawning, and attachment.
//!
//! The engine runs in daemon mode (`KYREX_DAEMON=1`) as a detached process
//! that hosts a localhost TCP socket. Its lifecycle is decoupled from the
//! IDE: closing the app does not kill it, and a restarted IDE reattaches to
//! the live engine (replaying everything it missed) instead of spawning a
//! second one.
//!
//! The control-file key below MUST mirror `kyrex_engine/daemon_bridge.py`
//! (FNV-1a 64 of the normalized workspace path, 16 lowercase hex chars).

use serde::Deserialize;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Debug, Clone, Deserialize)]
pub struct DaemonInfo {
    pub pid: u32,
    pub port: u16,
}

/// Must mirror `normalize_workspace` + `_fnv1a64` in daemon_bridge.py.
fn normalize_workspace(workspace: &str) -> String {
    let p = workspace.replace('\\', "/");
    let trimmed = p.trim_end_matches('/');
    if trimmed.is_empty() { "/".to_string() } else { trimmed.to_string() }
}

fn fnv1a64(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

pub fn daemon_key(workspace: &str) -> String {
    format!("{:016x}", fnv1a64(normalize_workspace(workspace).as_bytes()))
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
}

pub fn control_file_path(workspace: &str) -> Option<PathBuf> {
    home_dir().map(|home| {
        home.join(".kyrex")
            .join("daemons")
            .join(format!("{}.json", daemon_key(workspace)))
    })
}

pub fn read_daemon_info(workspace: &str) -> Option<DaemonInfo> {
    let path = control_file_path(workspace)?;
    let raw = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&raw).ok()
}

pub fn remove_stale_control_file(workspace: &str) {
    if let Some(path) = control_file_path(workspace) {
        let _ = std::fs::remove_file(path);
    }
}

/// Resolves the bundled engine sidecar binary. Tauri places external binaries
/// next to the app executable, either bare ("kyrex-engine") or with the
/// platform target-triple suffix ("kyrex-engine-x86_64-unknown-linux-gnu").
pub fn resolve_engine_binary() -> Result<PathBuf, String> {
    let exe = std::env::current_exe().map_err(|e| format!("failed to resolve app exe: {e}"))?;
    let dir = exe
        .parent()
        .ok_or("app executable has no parent directory")?
        .to_path_buf();

    let exact = ["kyrex-engine", "kyrex-engine.exe"];
    for name in exact {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Ok(candidate);
        }
    }

    if let Ok(entries) = std::fs::read_dir(&dir) {
        let mut candidates: Vec<PathBuf> = entries
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| {
                let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                (name.starts_with("kyrex-engine-") && !name.ends_with(".d"))
                    || name == "kyrex-engine"
            })
            .collect();
        candidates.sort();
        if let Some(first) = candidates.first() {
            return Ok(first.clone());
        }
    }

    Err(format!(
        "kyrex-engine sidecar binary not found next to {}",
        exe.display()
    ))
}

/// Spawns the engine in detached daemon mode. The process deliberately
/// outlives the IDE: own process group on Unix, DETACHED_PROCESS on Windows,
/// stdio routed to a log file instead of the (short-lived) app.
pub fn spawn_detached_daemon(workspace: &str) -> Result<(), String> {
    let engine_bin = resolve_engine_binary()?;

    let key = daemon_key(workspace);
    let log_path = std::env::temp_dir()
        .join(format!("kyrex-daemon-{}.log", key));

    let mut cmd = std::process::Command::new(&engine_bin);
    cmd.current_dir(workspace)
        .env("KYREX_SURFACE", "Kyrex IDE")
        .env("KYREX_VSCODE", "1")
        .env("KYREX_DAEMON", "1")
        .env("WORKSPACE_ROOT", workspace);

    // Stdout + stderr both land in one append-mode log file (the tee'd
    // engine output plus tracebacks). One open, cloned for both streams.
    let log = std::fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(&log_path)
        .map_err(|e| format!("failed to open daemon log {log_path:?}: {e}"))?;
    let log_clone = log
        .try_clone()
        .map_err(|e| format!("failed to clone daemon log handle: {e}"))?;
    cmd.stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::from(log))
        .stderr(std::process::Stdio::from(log_clone));

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        cmd.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP);
    }

    cmd.spawn()
        .map_err(|e| format!("failed to spawn engine daemon: {e}"))?;
    Ok(())
}

/// Waits (without blocking the async runtime) for the daemon to publish its
/// control file, then returns it.
pub async fn wait_for_daemon_info(workspace: &str, timeout_ms: u64) -> Result<DaemonInfo, String> {
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);
    loop {
        if let Some(info) = read_daemon_info(workspace) {
            return Ok(info);
        }
        if tokio::time::Instant::now() >= deadline {
            return Err("engine daemon did not publish its control file in time".into());
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_matches_python_reference() {
        // Cross-checked against daemon_bridge._fnv1a64 / daemon_key.
        assert_eq!(daemon_key("/a/b"), daemon_key("/a/b/"));
        assert_eq!(daemon_key("C:\\repo\\sub"), daemon_key("C:/repo/sub"));
        assert_ne!(daemon_key("/ws/a"), daemon_key("/ws/b"));
        // FNV-1a 64 of "foobar" is a published reference vector.
        assert_eq!(fnv1a64(b"foobar"), 0x85944171f73967e8);
    }
}
