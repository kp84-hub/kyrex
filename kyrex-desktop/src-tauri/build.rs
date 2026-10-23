fn main() {
    println!("cargo:rerun-if-env-changed=KYREX_DESKTOP_CHAT_URL");
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "toggle_chat",
            "open_chat",
            "show_settings",
            "drag_launcher",
            "launcher_activity",
            "companion_status",
            "set_option",
            "start_login",
        ]),
    ))
    .expect("build companion capabilities");
}
