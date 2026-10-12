# Kyrex Voice v0.7 — Android companion

Say **Hey Kyrex** (hey kai-rex), wait for the tone, then speak one request.
**Talk now** bypasses wake detection. The app uses the same saved voice
conversation and shared reply-voice settings as Kyrex Chat on PC and phone.

Setup, privacy, device switching and the Windows package are documented in
[the unified Voice guide](../kyrex-voice/README.md). Configure the speech key
and reply voice once in **Kyrex Chat → Connections → Voice**. This phone never
receives the provider key. The previous phone-only speech-key setting is
removed; v0.3 keys are not automatically uploaded to Cloud.

Wake audio stays local with the checksum-pinned English Gigaspeech 3.3M model
and sherpa-onnx v1.13.8. Android handles command transcription, using its own
speech service when on-device recognition is unavailable. Text goes to the
ordinary Overwatcher Chat path. AI reply WAVs are temporary private-cache files,
removed after playback or Stop and cleaned on service startup.

Listening requires explicit Start, microphone permission and a foreground
service. Stop is available in the app and notification. It uses a CPU wake
lock, consumes battery, has no boot receiver, and never silently restarts.
Starting on another device transfers listening; stale requests are rejected
and this service stops when the server confirms the session was revoked,
stopped, expired or taken over. A temporary connection failure pauses microphone
capture and checks the same lease for up to 90 seconds. Successful renewal
resumes the already user-started wake session; it never claims ownership again
or resends a command. Only an unchanged server lease may recover during the
90-second late-heartbeat grace; command and speech routes still require a live
lease. Longer outages stop listening with a connection-specific error.
Stop also suppresses late playback
and does not repeat or cancel a task already sent to Chat.

Build: `python3 prepare_assets.py`, then Gradle 8.11.1
`:app:testDebugUnitTest :app:lintDebug :app:assembleDebug` with Android SDK 36.
The Android workflow also exercises scoped Cloud credentials and the actual
wake model using deterministic Piper Amy samples at both thresholds. Those
samples are smoke checks, not real-phone accuracy or battery measurements.

JNI wrappers and native libraries are from k2-fsa/sherpa-onnx v1.13.8 under
Apache-2.0; see SHERPA-LICENSE and packaged ONNX notices. Model:
`sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01`. Generated assets are
ignored by git and rebuilt from fixed upstream archive checksums.

## Voice follow-ups

After a successful spoken reply, wait for the beep and speak again without the
wake phrase. The shared Follow-up listening setting in Chat chooses Off, 8 seconds
(default), or 15 seconds. Silence returns to wake listening, or ends a Talk now
session. Reply playback is not recorded; Stop and confirmed session loss cancel
follow-up capture. A failed command or failed speech playback does not start a
follow-up. Install v0.7 to use this behavior in the Android companion.
