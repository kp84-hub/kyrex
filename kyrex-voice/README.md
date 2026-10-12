# Kyrex Voice v0.4 — browser, Android and Windows

Voice is part of Kyrex Chat. **Talk** records one command; **Hey Kyrex** enables
local wake detection while Chat is visible. Both open the account's saved
**Voice · The Overwatcher** conversation and use its ordinary tools, tasks and
approval controls. Wake detection pauses during command capture and replies.

In **Connections → Voice**, save an OpenAI API key once and choose Cedar or
Marin for AI-generated replies. The key is encrypted on Kyrex Cloud, never
returned to clients, and never put in prompts. Command transcription uses
`gpt-4o-mini-transcribe`; shared replies use `gpt-4o-mini-tts`. API usage is
separate from a ChatGPT subscription. Device voice is also available, but it
uses each device's own voice. Browser and Windows transcription still need the
saved speech key. Removing the key disables those transcription paths.

Only the command recording goes to the transcription provider. Wake audio
stays local and is discarded. Android transcribes commands using its speech
service, which may be online. Browser and Windows send bounded mono 16 kHz PCM
WAV commands, at most 30 seconds; Cloud forwards them without saving audio.
Replies use bounded, temporary WAV audio and no automatic network retries.

## One active device

Listening is explicit and never starts on login, launch or boot. Starting on
another device transfers the account's listening lease. The old client stops
on its next heartbeat, and all stale transcription/chat/playback requests are
rejected immediately. A lease expires after 45 seconds without a heartbeat.
Only one voice turn may be submitted at a time, checked atomically before Chat
execution. Switching is blocked while a submitted voice turn is still running.
Stop releases the microphone and playback; it does not cancel a task already
accepted by Chat. A disconnected submission is not replayed automatically.

The browser stops when hidden or closed. For screen-off phone listening, use
the Android companion; for listening with the PC browser closed, use Windows.
Different native devices have separate revocable credentials and can remain
paired together. Disconnect one device or all devices in Connections.

## Android

Install **Kyrex-Voice-v0.4.apk**. In Chat, choose **Pair voice phone**. Enter
the server and ten-minute code in the app. Tap Start listening or Talk now.
Shared voice settings opens the same account settings in Chat. Phone commands
use Android transcription; replies use the shared voice selection.

The APK targets ARM64 Android 9+, with a user-started microphone foreground
service and explicit Stop. It does not start on boot. Preview CI signing keys
can change, so updating may require uninstalling and pairing again. Upgrade
old voice apps before using the v0.4 server: legacy credentials migrate, but
old clients without listening leases are asked to update before sending.

## Windows

Download and run **Kyrex-Voice-v0.4.exe** on Windows x64. No Python installation
is required. Choose **Pair Windows PC** in Chat, paste the server and code, then
Start listening or Talk now. Closing the window hides it in the system tray;
the tray menu includes Stop and Quit. Launching it does not start listening.
Windows microphone privacy settings must allow desktop apps.

The device credential is sealed using Windows DPAPI for the logged-in user.
The executable is a preview without a code-signing certificate. It is not an
installer and adds no startup entry. Native microphone accuracy, playback,
noise and Windows sleep/lock behavior still require an actual-PC test.

## Build and validation

- Cloud: run `test_voice_api.py` and `test_unified_voice.py` with the existing
  Cloud test environment. Tests use fake provider responses, no paid API calls.
- Browser: `npm ci`, the Chat recovery workflow and `npm run build`.
  `build_web_wake.py` compiles the checksum-pinned sherpa-onnx v1.13.8 source
  and English Gigaspeech 3.3M model using emsdk 4.0.23. Both Cloud Dockerfiles
  compile/copy the runtime into Chat's static assets; no third-party CDN runs
  code in the user's browser. Generated WASM/data files are ignored by git.
- Android: `prepare_assets.py`, wake synthetic checks, lifecycle tests, lint
  and APK assembly in the Android workflow.
- Windows: install the pinned requirements, run `prepare_assets.py` and the
  client tests, then the PyInstaller command in the unified-voice workflow.
  The packaged executable's `--self-test PATH` checks DPAPI, model loading and
  bundled dependencies without opening a microphone or calling a provider.

Synthetic wake speech does not establish real-person wake accuracy. Validate
Hey Kyrex on the actual PC/phone, device switching, Stop during processing,
network loss, room noise and pending approvals before relying on background
listening. Listening uses the microphone and consumes battery/CPU.

## Third-party code

Wake inference uses Apache-2.0 sherpa-onnx v1.13.8 (commit 11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf)
and its English Gigaspeech keyword model. Runtime packages include sherpa and
ONNX Runtime license/notices. The browser wrapper is compiled from the pinned
upstream source. Windows requirements and their installed package licenses
are included in the PyInstaller bundle.
