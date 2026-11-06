# Kyrex Voice — browser, Android and Windows

Voice is part of Kyrex Chat. **Talk** starts a voice conversation; **Kyrex** enables
local wake detection while Chat remains open. Both open the account's saved
**Voice · The Overwatcher** conversation and use its ordinary tools, tasks and
approval controls. Switching the visible Bot does not switch voice: it uses
the owned Overwatcher's model, instructions and tools in that dedicated thread.
The synthesized sound is the account's voice selection in Connections.

In the browser, **Interrupt reply** stops playback immediately and asks the
running Chat turn to stop. With **Listen for Kyrex** enabled, saying **Kyrex**
while it is working or speaking does the same. Wait for the next tone, then
speak your new request. Voice keeps its microphone and listening lease, but
does not submit another command until the previous turn has settled. If it
cannot confirm that, it stops and asks you to check voice chat. Interrupting
does not undo an action already completed. Closing the options panel or using
**Stop voice** still only stops listening/playback, without cancelling accepted
tasks. Wake detection pauses during command capture; during replies it listens
only for the interrupt keyword. Android and Windows companions retain their
existing controls; this interrupt UI and wake behavior apply to browser Chat.

After each successful spoken reply, a beep opens an eight-second follow-up
window. Speak without repeating the wake phrase to continue in the same saved
conversation and listening session. In **Connections → Voice → Follow-up
listening**, choose Off, 8 seconds (default), or 15 seconds. Silence is discarded
locally: wake sessions return to Kyrex; Talk sessions stop. Reply playback
pauses command capture; Stop cancels pending follow-ups immediately. Errors never
replay a command or open a follow-up window. Wake mode remains enabled across tab switches; browser suspension can still
pause its microphone. Android/Windows companions support native background listening.

In **Connections → Voice**, choose reply speech and PC transcription providers
independently. Settings and credentials belong to your account, not a particular
device or the server operator. Your chat model and tools remain unchanged.

| Purpose | Provider choices | Configuration |
| --- | --- | --- |
| Shared replies | Google Cloud Chirp 3 HD | Service account JSON, one of 30 English-US voices, speaking speed |
| Shared replies | OpenAI | Account credential; Cedar or Marin; speaking speed |
| Replies on each device | Device voice | No reply speech credentials; the device chooses its native voice |
| Browser/Windows transcription | Groq | Account credential; `whisper-large-v3` or `whisper-large-v3-turbo` |
| Browser/Windows transcription | OpenAI | Account credential; `gpt-4o-mini-transcribe` |
| Android transcription | Android speech service | Uses the phone's configured recognition service |

New account setup offers **Google Chirp 3 HD / Leda + Groq Whisper Large v3**,
the Chronic Cortex combination. Existing OpenAI settings keep their saved
provider, voice and encrypted key. Credentials are never returned to clients
or placed in prompts. Blank credential fields retain saved values; each
provider's credentials can be removed separately. No server-wide API key or
Firebase credential is silently used for another account.

### Configure Google and Groq

1. In your Google Cloud project, enable Cloud Text-to-Speech and billing.
   Create or use a service account authorized for that project and download its
   JSON key. See [Google authentication](https://docs.cloud.google.com/text-to-speech/docs/authentication).
   A Gemini API key alone is not a Google Cloud service account key.
2. Choose **Google Cloud · Chirp 3 HD** in Chat and paste that JSON into
   **Google Cloud service account JSON**. Choose a reply voice and speed.
3. Choose **Groq · Whisper** for PC transcription and enter your Groq API key.
   See [Groq speech documentation](https://console.groq.com/docs/speech-to-text).
4. Save, then use **Preview saved voice** to hear a short sample. Preview sends
   one fixed reply text to the selected speech provider; it never opens the
   microphone or submits a Chat task. Restart listening to apply settings.

Google/Groq usage is subject to their own billing and quotas; this setup does
not require OpenAI credits. Device replies still need the configured PC
transcription provider for browser/Windows input. Android does not use that
PC transcription key. Removing a required credential disables its path;
Kyrex does not silently switch providers or replay commands after failure.

Only the command recording goes to the transcription provider. Wake audio
stays local and is discarded. Android transcribes commands using its speech
service, which may be online. Browser and Windows send bounded mono 16 kHz PCM
WAV commands, at most 30 seconds; Cloud forwards them without saving audio.
Replies use bounded, temporary WAV audio and no automatic network retries.

### Reply pacing and spoken formatting

The shared voice Chat endpoint gives the existing model per-turn presentation
guidance: answer first in natural sentences, keep simple replies short, avoid
screen-oriented Markdown, and retain exact facts, uncertainty and approval
details. This does not change the selected Bot/model, stored owner request,
tool policy or ordinary text-Chat replies. Native deterministic tool/review
responses retain their established formatting and approval flow.

Browser Chat and Kbot prepare speech only from the completed Chat reply, never
provisional model/tool output. Markdown headings and list items get sentence
breaks; table cells retain their column labels. Links use their labels, code
stays in Chat, and long spoken replies end at a sentence or word boundary with
a pointer to the full transcript. This is deterministic formatting, not another
model request or a change to the saved answer, model, voice or speaking speed.

The first speech piece is at most 180 characters, with later pieces at most
400. Once a piece starts playing, the next piece can be synthesized and decoded
in advance. Only one lookahead is retained, which reduces inter-piece waiting
without synthesizing the entire reply ahead. Stop/interrupt discards prefetched
audio, fences late results and never replays the Chat command. One extra speech
piece may already have reached the provider when interrupted. Follow-up listening
still waits until all reply audio has finished. Browser/Kbot pacing and formatting
ship with Cloud/Chat and do not require a companion installer update; separate
native voice apps keep their existing text formatting and playback sequencing.

Shared Google speech reuses short-lived OAuth headers in a bounded process-local
cache keyed by owner and the current credential fingerprint. It refreshes before
expiry, serializes only refreshes for the same key, and invalidates the entry on
speech request failure without retrying. Every request still loads the owner's
current saved credentials and checks normal quotas; removal cannot be bypassed
by the cache. No audio, transcript, service-account key or token is cached on disk.
The Google optimization applies to every client using shared server speech.

These changes remove avoidable speech preparation waits; model/tool execution,
transcription, provider/network latency and audio delivery still determine live
response time. Controlled ordering/request-count checks are not live latency
measurements or a listening evaluation of the synthesized voice.

## One active device

Listening is explicit and never starts on login, launch or boot. If a companion
is already listening, Chat's Talk/Kyrex buttons show its name and leave it running.
Choose **Use this browser** to switch deliberately. Browser microphone and wake
engine startup finish before taking over, so a permission or loading failure
keeps the companion's session intact. A normal browser start also refuses a
competing session that appeared during startup. Native Start listening still
transfers the account's listening lease. The old client stops
on its next heartbeat, and all stale transcription/chat/playback requests are
rejected immediately. A lease expires after 45 seconds without a heartbeat.
Only one voice turn may be submitted at a time, checked atomically before Chat
execution. Switching is blocked while a submitted voice turn is still running.
Stop releases the microphone and playback; it does not cancel a task already
accepted by Chat. A disconnected submission is not replayed automatically.

### Continuous browser wake listening

The voice button opens a focused voice popup with a blue holographic core,
short status text and phase-appropriate controls. Minimize or Escape returns
to Chat without ending listening; **Stop voice** ends the voice session. During
wake listening, **Talk now** bypasses the trigger on the same microphone and
lease. The popup supports phone and compact Kbot sizes, keyboard focus and
reduced motion. Connections voice configuration is unchanged.

The core uses a local canvas particle mesh, luminous ribbons and segmented
rings. It rotates and pulses during the existing `speaking` phase, then settles
after playback, interruption or Stop. This is a playback-state animation, not
an audio-frequency visualization. Input level gently changes the resting core.
Rendering is capped at 30 frames per second, pauses in hidden tabs, and stops
when the popup closes. Reduced motion uses a still image. No additional audio,
model/provider requests, libraries or changes to voice settings are required.

Browser wake detection includes model-tokenized compressed “-icks” endings for
faster pronunciation. Those additional paths use a stricter per-keyword 0.25
threshold; the existing paths keep 0.19. Tests include five synthetic speaking
rates and similar unrelated speech, without claiming real-person accuracy.
Microphone visual updates are limited to 10 Hz so rendering does not compete
with wake inference; every audio sample still reaches its existing capture or
local detection path.

Click **Listen for Kyrex** once to keep wake mode enabled until **Stop voice**,
sign-out, closing/navigating away from Chat, microphone loss or a session error.
It accepts kai-rex and key-rex pronunciations, with or without a greeting.
Silence after a wake returns to local wake listening without a transcription
request. Ordinary **Talk now** still stops on hiding the page.

Switching tabs no longer deliberately shuts down wake mode. Returning to Chat
renews only the same listening lease and resumes its existing audio context;
it never takes over another device, opens another microphone or repeats a
command. Stop and device switches fence late resume results. Permissions are
still explicit and listening does not start on login, reload or launch.

Browsers/operating systems can suspend background tabs, audio or timers.
An expired/switched lease stops listening rather than silently reclaiming it.
This is best-effort browser background listening, not guaranteed screen-off
or closed-browser wake support. For screen-off phone listening, use the
Android companion; for listening with the PC browser closed, use Windows.
Different native devices have separate revocable credentials and can remain
paired together. Disconnect one device or all devices in Connections.

## Android

Install **Kyrex-Voice-v0.8.apk**. In Chat, choose **Pair voice phone**. Enter
the server and ten-minute code in the app. Tap Start listening or Talk now.
Shared voice settings opens the same account settings in Chat. Phone commands
use Android transcription; replies use the shared voice selection.

The APK targets ARM64 Android 9+, with a user-started microphone foreground
service and explicit Stop. It does not start on boot. Preview CI signing keys
can change, so updating may require uninstalling and pairing again. Upgrade
old voice apps before using the v0.4 server: legacy credentials migrate, but
old clients without listening leases are asked to update before sending.

The v0.5 APK updates the privacy explanation for configurable speech providers.
The v0.4 phone and Windows clients remain compatible with shared Google speech:
the existing `engine: "openai"` wire value is retained as the legacy marker for
any shared reply voice, while `speech_provider` identifies the actual provider.
The server returns bounded WAV from the same `/api/voice/speech` endpoint.

## Windows

Download and run **Kyrex-Voice-v0.7.exe** on Windows x64. No Python installation
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

### Self-hosting and adding providers

Persist `KYREX_DATA_DIR` and set a stable `WEB_SESSION_SECRET` (or
`KYREX_PROVIDER_SECRETS_KEY`) before saving credentials. Keys are encrypted in
the owner-scoped voice SQLite store. Changing the encryption secret makes saved
credentials unreadable; users must save them again. Never commit credentials.
The Cloud images explicitly install `google-auth` for Google service account
authentication. No separate Google TTS or Groq SDK is required.

`voice_speech_providers.py` holds the voice/model registry and server-side
provider adapters. `VoiceSpeech` owns configuration, credential encryption and
quota enforcement; it exposes only status flags, choices and preferences.
To add a provider, register supported models/voices, implement the bounded
adapter, add its credential validation and UI label, and test routing/redaction.
Provider URLs are fixed by the adapter, never read from user-supplied JSON.
Google OAuth is restricted to Google's token endpoint and service-account keys.
Both token and speech responses are bounded and closed; credentials and provider
debug bodies are not exposed. Quota/credit errors are distinguished from temporary
throttles where the provider supplies a recognized code. No request is replayed.

Synthetic wake speech does not establish real-person wake accuracy. Validate
Kyrex on the actual PC/phone, device switching, Stop during processing,
network loss, room noise and pending approvals before relying on background
listening. Listening uses the microphone and consumes battery/CPU.

## Third-party code

Wake inference uses Apache-2.0 sherpa-onnx v1.13.8 (commit 11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf)
and its English Gigaspeech keyword model. Runtime packages include sherpa and
ONNX Runtime license/notices. The browser wrapper is compiled from the pinned
upstream source. Windows requirements and their installed package licenses
are included in the PyInstaller bundle.

## Windows connection recovery (v0.6)

Windows distinguishes a confirmed session switch/revoked pairing from a temporary
heartbeat timeout or server failure. A temporary failure pauses the microphone
and renews only the same lease for up to 90 seconds. A wake session then resumes
with fresh audio; partial commands are discarded. Talk/follow-up capture stops
rather than replaying a partial recording. Stop, revoked pairing, a new owner
session, or prolonged outage ends listening and cannot reclaim it automatically.
Only one listening session is active: use either the Windows companion or Chat's
browser voice. Starting browser/phone voice takes over the companion session.

The one-word wake phrase can confuse very similar names: the synthetic “Kyle Rex”
fixture can also trigger. Those cases are reported separately from passing
positive/unrelated-speech checks. Actual voice, room noise and device CPU/battery
performance still require physical-device validation. Wake audio stays local.
