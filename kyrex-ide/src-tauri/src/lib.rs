mod bridge;

use bridge::{EngineState, RaceState};
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // A second launch (e.g. the kyrex:// OAuth deep link) is funneled
            // here instead of opening a new window. Focus the existing window;
            // the deep-link plugin forwards the URL to the running onOpenUrl listener.
            use tauri::Manager;
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(EngineState::default())
        .manage(RaceState::default())
        .setup(|app| {
            let window = app
                .get_webview_window("main")
                .expect("main window is configured");
            window.maximize()?;
            window.set_focus()?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            bridge::start_engine,
            bridge::send_to_bridge,
            bridge::stop_engine,
            bridge::read_file_contents,
            bridge::write_file_contents,
            bridge::list_dir,
            bridge::save_workspace_config,
            bridge::load_workspace_config,
            bridge::run_wizard_step,
            bridge::list_sessions,
            bridge::save_session_config,
            bridge::load_session_config,
            bridge::start_race,
            bridge::diff_race_lane,
            bridge::merge_race_lane,
            bridge::kill_race,
            bridge::save_desktop_refresh_token,
            bridge::load_desktop_refresh_token,
            bridge::clear_desktop_refresh_token
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
