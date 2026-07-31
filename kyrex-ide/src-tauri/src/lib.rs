mod bridge;

use bridge::{EngineState, RaceState};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(EngineState::default())
        .manage(RaceState::default())
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
            bridge::kill_race
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
