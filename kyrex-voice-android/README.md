# Kyrex Voice v0.3 — Android phone preview

Say **Hey Kyrex**, wait for the tone, then speak one request. The Android
companion sends the recognized text to a saved **Voice · The Overwatcher**
conversation, speaks the answer, then resumes wake listening.

If the wake phrase misses, tap **Talk now**, wait for the tone, and speak the
request. Starting Talk now while stopped captures one turn and stops afterward;
using it during a wake session resumes that session after the reply.
The app displays live wake microphone activity, wake matches, the last recognized
request (in memory only), and speech/startup errors. Errors remain visible after
the service stops instead of being replaced by a generic Stopped label.

If microphone activity rises while you speak but wake matches remain zero, try
**Sensitive wake detection**, then Stop and Start listening to apply it. This
uses a lower keyword threshold and can also increase false wakes. Standard and
Sensitive modes are exercised by the synthetic checks; actual-phone accuracy
still requires testing. If activity stays at zero, check Android's microphone
privacy switch and close other recording apps. Talk now helps distinguish a
wake-model miss from unavailable speech recognition or a Cloud connection issue.

## Reply voice

v0.1/v0.2 use the phone's default Android text-to-speech engine. They do not
select a neural reply voice. v0.3 adds **Reply voice settings** in the phone app:
enter an OpenAI API key and choose Cedar or Marin. Replies are synthesized using
`gpt-4o-mini-tts` with a conversational delivery instruction. API billing is
separate from ChatGPT. This is AI-generated speech.

The key is encrypted with Android Keystore on the phone and is never sent to
Kyrex Cloud, shown again in the settings field, or placed in a model prompt.
Only the bounded reply text goes directly to `https://api.openai.com/v1/audio/speech`.
Wake audio remains local; command transcription still uses Android's speech
service. Temporary reply MP3s stay in the app's private cache and are deleted
after playback or Stop; leftover files are removed on service startup. There
are no automatic synthesis retries. When AI speech fails, an error remains
visible and the reply is available in Chat. Select **Phone voice** to remove
the speech key and return to Android speech. Forgetting pairing also clears it.

The app now puts the wake microphone meter, wake count and current reply voice
near the top of the screen. v0.3 replaces the multilingual phoneme model with the English Gigaspeech
keyword model and its matching BPE keyword tokens, accepting both “Hey Kyrex”
and “hey kai rex”. It passes the same synthetic positive/negative checks;
real-phone wake accuracy still needs testing. A working Talk now path does not by itself prove the wake AudioRecord
is receiving useful samples. Live cloud speech still needs an actual API key;
the automated tests use a fake provider response and do not spend API credits.

Wake spotting uses sherpa-onnx v1.13.8 locally with a checksum-pinned 3.3M English model.
The initial English pronunciation is “hey kai-rex”. The
model/engine support custom keywords; this release deliberately exposes one
phrase while its false-positive rate and battery use are tested on a phone.
Wake audio is neither uploaded nor retained. Android's on-device speech
recognizer is preferred when available; otherwise its configured speech
service transcribes the command. That service may use its own network.
Only text reaches Kyrex, using the ordinary bot/task/approval flow.

## Setup

1. Merge and deploy the voice server/UI change. Start your owned Overwatcher.
2. In Chat > Connections > Voice, select **Pair voice phone**.
3. Install the ARM64 APK. Enter your HTTPS server and the ten-minute voice code.
4. Tap **Start listening**, confirm the listening session and grant microphone
   permission. Android shows a listening notification with **Stop listening**.
5. Say “Hey Kyrex”, pause for the tone, then “Show my calendar tomorrow”.
6. Open **Voice · The Overwatcher** in Chat to inspect the transcript/result.

Listening holds a microphone foreground service and a partial CPU wake lock
until Stop. It uses battery; it is explicitly opt-in and has no boot receiver,
silent microphone restart or battery-exemption bypass. Android force-stop,
permission revocation and vendor power management may stop it. Spoken output
pauses wake spotting to avoid the speaker triggering the microphone. This
first release does not support interrupting a spoken reply with a wake phrase.
Actions requiring review still require that review in Chat. A voice credential
is separate from Messages and is restricted to its owner's fixed voice thread.
Re-pairing replaces the old credential; disconnect in Chat revokes it.
Network failures never automatically repeat a transcript, tool call or send.

## Build and checks

Run `python3 prepare_assets.py`, then Gradle 8.11.1
`:app:testDebugUnitTest :app:lintDebug :app:assembleDebug` with Android SDK 36.
The dedicated workflow also checks Cloud token scope/revocation and the actual
offline wake model against synthetic speech. Generated ONNX and native assets
are ignored by git; fixed upstream archive checksums reproduce the APK.

For the CPU wake smoke checks, install `sherpa-onnx==1.13.8`, `soundfile` and
`piper-tts==1.8.0`, then run `python3 validate_wake.py`. This uses the
checksum-pinned Piper Amy English voice with fixed synthesis for three wake speeds and eight
negative phrases. Piper and that test voice are not packaged in the APK.
Both thresholds run against the same cases.
The earlier espeak formant probes were inconsistent at faster speeds;
passing neural synthetic samples still does not establish human wake accuracy.

Before claiming hands-free reliability, test on the actual phone: screen off,
quiet and noisy rooms, unrelated speech/TV, repeated commands, microphone
permission revocation, network loss, Stop and recipient approval. The APK and
CPU model tests cannot establish Samsung battery behavior or your wake accuracy.

## Third-party code and model

Kotlin JNI wrappers are adapted from k2-fsa/sherpa-onnx tag v1.13.8,
commit 11afbd0, under Apache-2.0; see SHERPA-LICENSE. They retain their original
JNI package/class names. Native libraries come from the same release and the
model is sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01. ONNX Runtime's upstream
license and notices are included during asset preparation.
