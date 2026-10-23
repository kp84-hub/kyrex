use crate::{
    policy::{PendingLogin, REDIRECT},
    report_error, show_chat, status_changed, AppState,
};
use serde::Deserialize;
use tauri::{AppHandle, Manager};
use tauri_plugin_opener::OpenerExt;
use url::Url;

pub fn begin(app: &AppHandle) -> Result<(), String> {
    let state = app.state::<AppState>();
    let pending = PendingLogin::new();
    let url = pending.start_url(&state.origin);
    *state.pending.lock().unwrap() = Some(pending);
    if app.opener().open_url(url.as_str(), None::<&str>).is_err() {
        *state.pending.lock().unwrap() = None;
        return Err("Could not open your browser. Try sign-in again.".into());
    }
    Ok(())
}

#[derive(Deserialize)]
struct Tokens {
    access_token: String,
}

pub async fn complete(app: AppHandle, raw: String) -> Result<(), String> {
    let state = app.state::<AppState>();
    let url = Url::parse(&raw).map_err(|_| "Invalid sign-in response")?;
    // Validate before taking the attempt. A foreign/malformed deep link cannot
    // erase an in-progress login; a valid callback is consumed before networking.
    let (code, pending) = {
        let mut slot = state.pending.lock().unwrap();
        let code = slot
            .as_ref()
            .ok_or("Start sign-in from the Kyrex companion first.")?
            .callback_code(&url)?;
        (code, slot.take().unwrap())
    };
    let response = state.client.post(state.origin.join("/auth/desktop/exchange").unwrap())
        .json(&serde_json::json!({"code": code, "redirect_uri": REDIRECT, "code_verifier": pending.verifier}))
        .send().await.map_err(|_| "Could not reach Kyrex. Start sign-in again.")?;
    if !response.status().is_success() {
        return Err("Sign-in expired or was declined. Start sign-in again.".into());
    }
    let tokens: Tokens = response
        .json()
        .await
        .map_err(|_| "Kyrex returned an invalid sign-in response")?;
    let response = state
        .client
        .post(state.origin.join("/auth/desktop/session").unwrap())
        .bearer_auth(tokens.access_token)
        .send()
        .await
        .map_err(|_| "Could not finish sign-in. Start sign-in again.")?;
    if response.status() != reqwest::StatusCode::NO_CONTENT {
        return Err("Could not finish sign-in. Update the Kyrex server and try again.".into());
    }
    let value = response
        .headers()
        .get(reqwest::header::SET_COOKIE)
        .and_then(|v| v.to_str().ok())
        .ok_or("Kyrex did not return a browser session")?;
    let cookie =
        tauri::webview::Cookie::parse(value.to_owned()).map_err(|_| "Invalid browser session")?;
    if cookie.name() != "session"
        || cookie.value().is_empty()
        || cookie.http_only() != Some(true)
        || cookie.secure() != Some(true)
        || cookie.path() != Some("/")
        || cookie.domain().is_some()
    {
        return Err("Kyrex returned an unexpected session policy".into());
    }
    // Native-only, HttpOnly cookie. No token is persisted in preferences,
    // JavaScript, deep-link URLs, logs or an alternate credential store.
    let mut cookie = cookie.into_owned();
    cookie.set_domain(state.origin.host_str().unwrap().to_owned());
    let window = app
        .get_webview_window("chat")
        .ok_or("Chat window is unavailable")?;
    window
        .set_cookie(cookie)
        .map_err(|_| "Could not save your sign-in. Try again.")?;
    // Reading back also acts as a cookie-store barrier before the navigation.
    window
        .cookies_for_url(state.origin.clone())
        .map_err(|_| "Could not confirm sign-in")?;
    window
        .navigate(state.origin.clone())
        .map_err(|_| "Could not open Chat")?;
    state.status.lock().unwrap().account =
        "Signed in. Your existing Kyrex account is ready.".into();
    status_changed(&app);
    show_chat(&app, false);
    Ok(())
}

pub fn handle_callback(app: &AppHandle, raw: String) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(error) = complete(app.clone(), raw).await {
            report_error(&app, &error);
        }
    });
}
