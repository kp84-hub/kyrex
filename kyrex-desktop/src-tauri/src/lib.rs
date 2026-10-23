mod auth;
mod policy;

use serde::{Deserialize, Serialize};
use std::{path::PathBuf, sync::Mutex, time::Duration};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    webview::NewWindowResponse,
    AppHandle, Emitter, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;
use url::Url;

#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(default)]
struct Preferences {
    pinned: bool,
    alerts: bool,
    position: Option<[i32; 2]>,
}

#[derive(Clone, Serialize)]
struct Status {
    account: String,
    unread: u64,
    pinned: bool,
    autostart: bool,
    alerts: bool,
    shortcut_error: String,
}

struct AppState {
    origin: Url,
    client: reqwest::Client,
    preferences: Mutex<Preferences>,
    status: Mutex<Status>,
    pending: Mutex<Option<policy::PendingLogin>>,
    preferences_path: PathBuf,
}

fn local_control(window: &WebviewWindow) -> Result<(), String> {
    if !matches!(window.label(), "launcher" | "settings") {
        return Err("This action is only available from companion controls.".into());
    }
    Ok(())
}

fn save_preferences(state: &AppState) -> Result<(), String> {
    let content = serde_json::to_vec(&*state.preferences.lock().unwrap()).unwrap();
    let temporary = state.preferences_path.with_extension("tmp");
    std::fs::write(&temporary, content)
        .and_then(|_| std::fs::rename(temporary, &state.preferences_path))
        .map_err(|_| "Could not save companion preferences.".into())
}

fn status_changed(app: &AppHandle) {
    let state = app.state::<AppState>();
    let _ = app.emit_to(
        "settings",
        "companion-status",
        state.status.lock().unwrap().clone(),
    );
}

fn report_error(app: &AppHandle, message: &str) {
    show_settings_window(app);
    let _ = app.emit_to("settings", "companion-error", message);
}

fn show_settings_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("settings") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn position_bubble(app: &AppHandle) {
    let (Some(icon), Some(chat)) = (
        app.get_webview_window("launcher"),
        app.get_webview_window("chat"),
    ) else {
        return;
    };
    let (Ok(p), Ok(size), Ok(Some(monitor))) = (
        icon.outer_position(),
        icon.outer_size(),
        icon.current_monitor(),
    ) else {
        return;
    };
    let work = monitor.work_area();
    let placement = policy::bubble_placement(
        (p.x, p.y, size.width, size.height),
        (
            work.position.x,
            work.position.y,
            work.size.width,
            work.size.height,
        ),
        monitor.scale_factor(),
    );
    let size = tauri::PhysicalSize::new(placement.width, placement.height);
    // Move to the target monitor first so Windows applies its DPI before sizing.
    let _ = chat.set_position(tauri::PhysicalPosition::new(placement.x, placement.y));
    if chat.inner_size().ok() != Some(size) {
        let _ = chat.set_size(size);
    }
    let anchor = serde_json::json!({"side": placement.side, "y": placement.tail_y});
    let _ = chat.eval(&format!("window.__kyrexKbotAnchor = {anchor}; window.dispatchEvent(new Event('kyrex-kbot-anchor'));"));
}

fn hide_chat(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("chat") {
        let _ = window.hide();
    }
    let _ = app.emit_to("launcher", "companion-phase", "idle");
}

fn show_chat(app: &AppHandle, talk: bool) {
    position_bubble(app);
    if let Some(window) = app.get_webview_window("chat") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        if let Ok(title) = window.title() {
            if let Some(phase) = policy::mascot_phase(&title) {
                let _ = app.emit_to("launcher", "companion-phase", phase);
            }
        }
        let _ = window.eval("window.dispatchEvent(new Event('kyrex-kbot-open'));");
        if talk {
            // Input adapter only. VoiceControls owns microphone consent, device
            // leases and the existing task pipeline. This cannot grant a tool.
            let _ = window.eval("window.__kyrexDesktopTalkPending = Date.now(); window.dispatchEvent(new Event('kyrex-desktop-talk'));");
        }
    }
}

fn toggle_chat_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("chat") {
        if window.is_visible().unwrap_or(false) && !window.is_minimized().unwrap_or(false) {
            hide_chat(app);
        } else {
            show_chat(app, false);
        }
    }
}

#[tauri::command]
async fn toggle_chat(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    local_control(&window)?;
    toggle_chat_window(&app);
    Ok(())
}
#[tauri::command]
async fn open_chat(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    local_control(&window)?;
    show_chat(&app, false);
    Ok(())
}
#[tauri::command]
async fn show_settings(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    local_control(&window)?;
    show_settings_window(&app);
    Ok(())
}
#[tauri::command]
async fn drag_launcher(window: WebviewWindow) -> Result<(), String> {
    if window.label() != "launcher" {
        return Err("Only the floating icon can be dragged.".into());
    }
    window
        .start_dragging()
        .map_err(|_| "Could not move the floating icon.".into())
}
#[tauri::command]
async fn companion_status(window: WebviewWindow, app: AppHandle) -> Result<Status, String> {
    local_control(&window)?;
    Ok(app.state::<AppState>().status.lock().unwrap().clone())
}
#[tauri::command]
async fn set_option(
    window: WebviewWindow,
    app: AppHandle,
    name: String,
    enabled: bool,
) -> Result<Status, String> {
    local_control(&window)?;
    let state = app.state::<AppState>();
    match name.as_str() {
        "autostart" => {
            let manager = app.autolaunch();
            (if enabled {
                manager.enable()
            } else {
                manager.disable()
            })
            .map_err(|_| "Windows startup preference could not be changed.")?;
            state.status.lock().unwrap().autostart = manager.is_enabled().unwrap_or(false);
        }
        "pinned" | "alerts" => {
            let previous = state.preferences.lock().unwrap().clone();
            {
                let mut prefs = state.preferences.lock().unwrap();
                if name == "pinned" {
                    prefs.pinned = enabled;
                } else {
                    prefs.alerts = enabled;
                }
            }
            if let Err(error) = save_preferences(&state) {
                *state.preferences.lock().unwrap() = previous;
                return Err(error);
            }
            if name == "pinned" {
                if let Some(chat) = app.get_webview_window("chat") {
                    chat.set_always_on_top(enabled)
                        .map_err(|_| "Could not change Chat pinning.")?;
                }
                state.status.lock().unwrap().pinned = enabled;
            } else {
                state.status.lock().unwrap().alerts = enabled;
            }
        }
        _ => return Err("Unknown companion preference.".into()),
    }
    status_changed(&app);
    let result = state.status.lock().unwrap().clone();
    Ok(result)
}
#[tauri::command]
async fn start_login(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    local_control(&window)?;
    auth::begin(&app)
}

fn position_launcher(app: &AppHandle, reset: bool) {
    let Some(window) = app.get_webview_window("launcher") else {
        return;
    };
    let monitors = window.available_monitors().unwrap_or_default();
    let saved = if reset {
        None
    } else {
        app.state::<AppState>().preferences.lock().unwrap().position
    };
    // Keep the full launcher on a connected display; DPI changes/unplugging a
    // monitor cannot strand it offscreen. Tray's Reset position is an escape.
    let valid = saved.filter(|[x, y]| {
        monitors.iter().any(|monitor| {
            let p = monitor.position();
            let size = monitor.size();
            let extent = (84.0 * monitor.scale_factor()).ceil() as i32;
            *x >= p.x
                && *y >= p.y
                && *x + extent <= p.x + size.width as i32
                && *y + extent <= p.y + size.height as i32
        })
    });
    if let Some([x, y]) = valid {
        let _ = window.set_position(tauri::PhysicalPosition::new(x, y));
    } else if let Some(monitor) = window.primary_monitor().ok().flatten() {
        let p = monitor.position();
        let size = monitor.size();
        let scale = monitor.scale_factor();
        let _ = window.set_position(tauri::PhysicalPosition::new(
            p.x + size.width as i32 - (110.0 * scale) as i32,
            p.y + size.height as i32 - (180.0 * scale) as i32,
        ));
    }
}

fn open_external(app: &AppHandle, url: &Url) {
    if policy::external_link(url) {
        let opener = app.clone();
        let url = url.to_string();
        tauri::async_runtime::spawn(async move {
            if opener.opener().open_url(url, None::<&str>).is_err() {
                report_error(&opener, "Could not open the link in your browser.");
            }
        });
    }
}

fn create_chat(app: &AppHandle) -> tauri::Result<()> {
    let origin = app.state::<AppState>().origin.clone();
    let pinned = app.state::<AppState>().preferences.lock().unwrap().pinned;
    let nav_app = app.clone();
    let nav_origin = origin.clone();
    let popup_app = app.clone();
    let initial = if std::env::args().any(|arg| arg == "--smoke-test") {
        Url::parse("about:blank").unwrap()
    } else {
        policy::bubble_url(&origin)
    };
    WebviewWindowBuilder::new(app, "chat", WebviewUrl::External(initial))
        .title("Kbot")
        .inner_size(320.0, 300.0)
        .decorations(false)
        .transparent(true)
        .background_color(tauri::window::Color(0, 0, 0, 0))
        .shadow(false)
        .resizable(false)
        .skip_taskbar(true)
        .always_on_top(pinned)
        .visible(false)
        .on_navigation(move |url| {
            if policy::bubble_close(&nav_origin, url) {
                hide_chat(&nav_app);
                return false;
            }
            if policy::same_origin(&nav_origin, url) {
                if url.path() == "/auth/login" {
                    if let Err(error) = auth::begin(&nav_app) {
                        report_error(&nav_app, &error);
                    }
                    return false;
                }
                return true;
            }
            open_external(&nav_app, url);
            false
        })
        .on_document_title_changed(|window, title| {
            let state = window.state::<AppState>();
            if !window
                .url()
                .is_ok_and(|url| policy::same_origin(&state.origin, &url))
            {
                return;
            }
            if let Some(phase) = policy::mascot_phase(&title) {
                position_bubble(window.app_handle());
                if window.is_visible().unwrap_or(false) {
                    let _ = window.emit_to("launcher", "companion-phase", phase);
                }
            }
        })
        .on_new_window(move |url, _| {
            // about:blank reservations are denied; Chat offers its existing
            // Continue connecting link, which opens the actual URL externally.
            open_external(&popup_app, &url);
            NewWindowResponse::Deny
        })
        // Use WebView2's normal microphone prompt. Never auto-grant capture,
        // notifications, screen access or permissions to another origin.
        .build()?;
    Ok(())
}

#[derive(Deserialize)]
struct InboxCount {
    unread: u64,
}

async fn poll_inbox(app: &AppHandle) {
    let state = app.state::<AppState>();
    let Some(chat) = app.get_webview_window("chat") else {
        return;
    };
    let cookie = match chat.cookies_for_url(state.origin.clone()) {
        Ok(cookies) => cookies
            .into_iter()
            .find(|c| c.name() == "session" && !c.value().is_empty()),
        Err(_) => return,
    };
    let count = if let Some(cookie) = cookie {
        let response = match state
            .client
            .get(state.origin.join("/api/notifications").unwrap())
            .header(
                reqwest::header::COOKIE,
                format!("session={}", cookie.value()),
            )
            .send()
            .await
        {
            Ok(response) => response,
            Err(_) => return,
        };
        if response.status() == reqwest::StatusCode::UNAUTHORIZED {
            None
        } else if response.status().is_success() {
            match response.json::<InboxCount>().await {
                Ok(inbox) => Some(inbox.unread),
                Err(_) => return,
            }
        } else {
            return;
        }
    } else {
        None
    };
    let unread = count.unwrap_or(0).min(9999);
    let (previous, alerts) = {
        let mut status = state.status.lock().unwrap();
        let previous = status.unread;
        status.unread = unread;
        status.account = if count.is_some() {
            "Signed in. Your existing Kyrex account is ready."
        } else {
            "Sign in with your existing Kyrex account."
        }
        .into();
        (previous, status.alerts)
    };
    let _ = app.emit_to("launcher", "companion-unread", unread);
    status_changed(app);
    if let Some(tray) = app.tray_by_id("kyrex") {
        let _ = tray.set_tooltip(Some(if unread > 0 {
            format!("Kyrex · {unread} unread")
        } else {
            "Kyrex Chat".into()
        }));
    }
    if alerts && unread > previous && !chat.is_focused().unwrap_or(false) {
        // No message content, health information, subject or sender in toasts.
        let _ = app
            .notification()
            .builder()
            .title("Kyrex Chat")
            .body("You have new activity. Open Kyrex to review it.")
            .show();
    }
}

pub fn run() {
    let shortcut_plugin = tauri_plugin_global_shortcut::Builder::new().build();
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| show_chat(app, false)))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(shortcut_plugin)
        .plugin(tauri_plugin_autostart::Builder::new().args(["--background"]).build())
        .plugin(tauri_plugin_notification::init())
        .on_window_event(|window, event| match event {
            tauri::WindowEvent::CloseRequested { api, .. } => {
                api.prevent_close();
                if window.label() == "chat" { hide_chat(window.app_handle()); } else { let _ = window.hide(); }
            }
            tauri::WindowEvent::Moved(position) if window.label() == "launcher" => {
                if let Some(state) = window.app_handle().try_state::<AppState>() {
                    state.preferences.lock().unwrap().position = Some([position.x, position.y]);
                }
                if window.app_handle().get_webview_window("chat").is_some_and(|chat| chat.is_visible().unwrap_or(false)) {
                    position_bubble(window.app_handle());
                }
            }
            _ => {}
        })
        .setup(|app| {
            let origin = policy::chat_origin(option_env!("KYREX_DESKTOP_CHAT_URL").unwrap_or("https://chat.kyrex.dev"))?;
            let config_dir = app.path().app_config_dir()?; std::fs::create_dir_all(&config_dir)?;
            let path = config_dir.join("companion.json");
            let prefs: Preferences = std::fs::read(&path).ok().and_then(|raw| serde_json::from_slice(&raw).ok()).unwrap_or_default();
            app.manage(AppState {
                origin, client: reqwest::Client::builder().https_only(true)
                    .redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(20)).build()?,
                status: Mutex::new(Status { account: "Sign in with your existing Kyrex account.".into(), unread: 0,
                    pinned: prefs.pinned, alerts: prefs.alerts, autostart: app.autolaunch().is_enabled().unwrap_or(false), shortcut_error: String::new() }),
                preferences: Mutex::new(prefs), pending: Mutex::new(None), preferences_path: path,
            });
            create_chat(app.handle())?;
            let open = MenuItem::with_id(app, "open", "Open Kyrex Chat", true, None::<&str>)?;
            let talk = MenuItem::with_id(app, "talk", "Talk to Kyrex", true, None::<&str>)?;
            let settings = MenuItem::with_id(app, "settings", "Companion settings / Sign in", true, None::<&str>)?;
            let launcher = MenuItem::with_id(app, "launcher", "Show / hide floating icon", true, None::<&str>)?;
            let reset = MenuItem::with_id(app, "reset", "Reset icon position", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Kyrex", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &talk, &settings, &launcher, &reset, &quit])?;
            TrayIconBuilder::with_id("kyrex").icon(app.default_window_icon().unwrap().clone())
                .tooltip("Kyrex Chat").menu(&menu).show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_chat(app, false), "talk" => show_chat(app, true),
                    "settings" => show_settings_window(app),
                    "launcher" => if let Some(window) = app.get_webview_window("launcher") {
                        if window.is_visible().unwrap_or(false) { let _ = window.hide(); }
                        else { position_launcher(app, false); let _ = window.show(); }
                    },
                    "reset" => { position_launcher(app, true); if let Some(w) = app.get_webview_window("launcher") { let _ = w.show(); } },
                    "quit" => { let _ = save_preferences(&app.state::<AppState>()); app.exit(0); },
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if matches!(event, TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. }) {
                        toggle_chat_window(tray.app_handle());
                    }
                }).build(app)?;
            for (keys, talk) in [("Ctrl+Alt+Space", false), ("Ctrl+Alt+K", true)] {
                if app.global_shortcut().on_shortcut(keys, move |app, _, event| {
                    if event.state == ShortcutState::Pressed {
                        if talk { show_chat(app, true); } else { toggle_chat_window(app); }
                    }
                }).is_err() {
                    app.state::<AppState>().status.lock().unwrap().shortcut_error =
                        "A Kyrex shortcut is already used by another app. The icon and tray still work.".into();
                }
            }
            let handle = app.handle().clone();
            app.deep_link().on_open_url(move |event| {
                let urls = event.urls();
                if urls.len() == 1 { auth::handle_callback(&handle, urls[0].to_string()); }
            });
            position_launcher(app.handle(), false);
            app.get_webview_window("launcher").unwrap().show()?;
            if std::env::args().any(|arg| arg == "--smoke-test") {
                // Exercise actual WebView2 windows/tray, without loading Chat,
                // authenticating, touching an account, or opening a microphone.
                let handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    tokio::time::sleep(Duration::from_secs(2)).await;
                    let chat = handle.get_webview_window("chat").unwrap();
                    let launcher = handle.get_webview_window("launcher").unwrap();
                    let settings = handle.get_webview_window("settings").unwrap();
                    let mut passed = handle.tray_by_id("kyrex").is_some()
                        && launcher.is_always_on_top().unwrap_or(false)
                        && !chat.is_visible().unwrap_or(true);
                    // Probe a read-only command from the unprivileged Chat
                    // window. It must be absent or rejected, never authorized.
                    let _ = chat.eval("window.__kyrexSmokeDenied = !window.__TAURI_INTERNALS__; if (window.__TAURI_INTERNALS__) window.__TAURI_INTERNALS__.invoke('companion_status').then(() => window.__kyrexSmokeDenied = false, () => window.__kyrexSmokeDenied = true);");
                    show_chat(&handle, false);
                    passed &= chat.is_visible().unwrap_or(false)
                        && !chat.is_decorated().unwrap_or(true)
                        && !chat.is_resizable().unwrap_or(true);
                    let _ = chat.close();
                    tokio::time::sleep(Duration::from_millis(300)).await;
                    passed &= handle.get_webview_window("chat").is_some() && !chat.is_visible().unwrap_or(true);
                    toggle_chat_window(&handle);
                    passed &= chat.is_visible().unwrap_or(false);
                    show_settings_window(&handle);
                    passed &= settings.is_visible().unwrap_or(false);
                    passed &= handle.webview_windows().len() == 3;
                    tokio::time::sleep(Duration::from_secs(1)).await;
                    let (tx, rx) = std::sync::mpsc::channel();
                    let _ = settings.eval_with_callback("document.querySelector('#account')?.textContent || ''", move |value| { let _ = tx.send(value); });
                    passed &= rx.recv_timeout(Duration::from_secs(5)).is_ok_and(|value| value.contains("Sign in with your existing"));
                    let (tx, rx) = std::sync::mpsc::channel();
                    let _ = chat.eval_with_callback("window.__kyrexSmokeDenied === true", move |value| { let _ = tx.send(value); });
                    passed &= rx.recv_timeout(Duration::from_secs(5)).is_ok_and(|value| value == "true");
                    handle.exit(if passed { 0 } else { 1 });
                });
                return Ok(());
            }
            if !std::env::args().any(|arg| arg == "--background") { show_settings_window(app.handle()); }
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                loop { poll_inbox(&handle).await; tokio::time::sleep(Duration::from_secs(20)).await; }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![toggle_chat, open_chat, show_settings, drag_launcher,
            companion_status, set_option, start_login])
        .run(tauri::generate_context!())
        .expect("could not start Kyrex companion");
}
