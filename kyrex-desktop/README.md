# Kyrex Chat desktop companion

A Windows 10/11 companion that keeps a small, draggable Kyrex icon above the
desktop. Click it to open or hide a compact Chat window. It loads the existing
hosted Kyrex Chat UI and uses the same account, chats, bots, connectors, reviews
and task execution as the website. This is separate from the Kyrex IDE and does
not bundle a local agent engine.

## Install and use

1. Deploy this change to the existing Cloud/Chat service first. It adds the
   desktop session handoff and the Talk shortcut listener.
2. In GitHub Actions, open **Build Kyrex Chat companion**, choose a successful
   Windows run and download **Kyrex-Chat-Windows** from its artifacts. Unzip it
   and run the per-user setup `.exe`. The initial build is unsigned.
3. Choose **Sign in** in Companion settings. Finish GitHub sign-in in your
   normal browser and allow the `kyrex-chat` link to return to the companion.
   Use the same account as the website; no provider API key goes into the shell.
4. Click the icon to chat; drag it to move it. Right-click the floating icon
   for settings. The tray menu can open Chat, start Talk, hide/show the floating
   icon, reset its position, or quit.

| Control | Behavior |
| --- | --- |
| Ctrl + Alt + Space | Open/hide the existing Chat panel |
| Ctrl + Alt + K | Open Chat and start **Talk now**, if voice is configured and idle |
| Keep Chat on top | Optional pinning for the Chat panel; the floating icon is always on top |
| Start with Windows | Explicit opt-in; starts the icon in the background |
| Desktop alerts | Explicit opt-in; generic alerts for an increased unread count, without message content |
| Close Chat | Hide its existing window; accepted server tasks keep running |
| Quit Kyrex | Exit the companion; server tasks keep running |

Notifications update while the companion is running, about every 20 seconds.
The icon shows an unread badge and the tray tooltip shows its count. Windows
notification settings can suppress alerts. After a session expires, sign in
again from the tray's **Companion settings / Sign in** action.

Voice uses the existing Chat microphone permission, speech settings, active
device lease and conversation. It stops when the panel is hidden; this app
does not add screen-off/background wake listening. If another device is
listening, the normal explicit handoff UI appears. Missing speech credentials
still need to be saved in Chat's Voice settings.

Connections open consent pages in the normal browser. If a reserved blank
popup cannot open, Chat shows **Continue connecting**; click that link and
return after consent. A connection's owner binding and callback validation stay
on the server. This shell cannot inspect a website underneath it, take a
screenshot, or control other applications.

## Development

On Windows install Node 24, Rust stable, the Visual Studio C++ build tools and
WebView2 ([Tauri prerequisites](https://v2.tauri.app/start/prerequisites/)). Then:

```powershell
cd kyrex-desktop
npm ci
npm test
npm run tauri dev
```

For an installer, run `npm run tauri build -- --locked --bundles nsis`. No
Python sidecar is needed. The Windows workflow runs native policy tests,
Clippy, frontend tests, builds the installer, and starts the real WebView2
windows/tray with `--smoke-test` without signing in, loading the remote app or
opening a microphone. The browser-voice test remains a controlled UI test;
test microphone permissions, browser consent and sign-in on a real PC before
calling those live-verified.

The default Chat origin is `https://chat.kyrex.dev`. For a separate deployment,
set `KYREX_DESKTOP_CHAT_URL` to its HTTPS origin **when building**. Paths,
embedded credentials, queries and non-HTTPS addresses are rejected. The origin
cannot be changed by a chat message or remote page. An explicit server
`KYREX_DESKTOP_REDIRECT_ALLOWLIST` must include `kyrex-chat://auth/callback` (keep
`kyrex://auth/callback` for the IDE). Without that override, both are enabled.
Chat's desktop login reuses the website's configured `/auth/callback`; it
doesn't require changing the GitHub OAuth App or the IDE's callback URL.

## Replace the icon

The temporary K is intentionally separate from Chat's branding. Replace
`public/companion.svg` with the new artwork for the floating icon and settings.
Use a square design with transparent padding. For the tray, taskbar and
installer, export a square PNG and run:

```powershell
npm run tauri icon path/to/your-icon.png
```

Rebuild the app. `src-tauri/icons/icon.png` and `icon.ico` are the desktop
assets; the initial versions reuse the repository's existing Kyrex icon.

## Security and lifecycle

- Only the bundled `launcher` and `settings` documents can invoke the narrow
  companion controls. The remote `chat` WebView has **no native capabilities**.
  No filesystem, shell, screen-capture, clipboard, general URL opener or device
  commands are exposed to it.
- Sign-in uses the existing desktop OAuth flow, fresh state, S256 PKCE, a
  fixed distinct `kyrex-chat` callback, and one-use short-lived handoff codes.
  Native code converts the bearer token family into one existing durable
  browser session, revoking that family. No refresh token is saved by this app.
- The session is an HttpOnly, Secure, SameSite=Lax cookie in WebView2's app
  profile. Preferences contain only non-secret UI choices and position. The
  server stores the existing session HMAC, not its bearer cookie. Treat the
  Windows profile as private, as with a browser profile.
- The native HTTP client is HTTPS-only, timeout-bounded and refuses redirects.
  It sends the session only to the compiled Chat origin. External links allow
  HTTPS only and open in the user's browser, without a native bridge.
- Closing/hiding Chat doesn't submit, approve, cancel, or replay a task. Task
  policy and ownership remain on the existing server. Restart recovery has the
  same limits as the web client; the companion does not introduce another queue.
