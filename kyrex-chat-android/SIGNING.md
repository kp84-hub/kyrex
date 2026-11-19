# Retained Android signing and update recovery

Android accepts an ordinary in-place update only with a compatible signing
certificate and version. A generic "App not installed" message is not itself a
certificate diagnosis. The v0.5 test artifact from Actions run `41567913349`
was built without `KYREX_CHAT_ANDROID_KEYSTORE_B64`; that runner used a temporary
debug key. Do not promise that artifact can update an existing installation.

## Recover an existing installation first

1. Keep the existing app installed while diagnosing. Prefer its original
   retained signing keystore if one exists, then sign the new build with it.
   Check the private local build setup or approved secret storage; never ask
   for a private key or base64 keystore in Chat, a Git issue or a PR.
2. `signing-fingerprint.txt` is public certificate metadata. It can be compared
   between old and new APKs using `apksigner verify --print-certs`. A certificate
   or APK cannot recover its private signing key.
3. For an exact install failure, an owner with Android platform tools can use
   `adb install -r /path/to/app-debug.apk` from their trusted computer and inspect
   the package-manager error. Do not uninstall, clear storage, relax Android
   security settings or blindly retry as a diagnosis.
4. If the old signing key is unavailable, choose and retain the new key before
   reinstalling once. Explain that uninstalling removes local Android app data,
   including native credentials and preferences. Cloud chat history is separate;
   Health, Messages and Voice may need reconnection and permission setup. Keep
   old standalone companions until the new app passes acceptance checks, with
   their background sessions stopped to avoid competing connections.

Installing an APK requires the owner's explicit action on their phone. If a new
APK still fails, inspect the exact failure rather than assuming signing is the
only possible cause (version downgrade, platform, package and storage failures
have separate remedies).

## Configure one retained test key

Use the existing key when available. If a new key is necessary, generate it on
the owner's trusted computer outside the repository. The current Gradle signing
configuration expects the standard debug-format alias and passwords below;
security depends on keeping the private keystore restricted, not on those
conventional debug passwords. This is the sideloaded test-app setup, not a Play
Store release-signing configuration.

Example for the owner's WSL/Linux terminal with JDK `keytool` installed:

```sh
mkdir -p "$HOME/.config/kyrex-signing"
chmod 700 "$HOME/.config/kyrex-signing"
keytool -genkeypair \
  -keystore "$HOME/.config/kyrex-signing/chat-test.keystore" \
  -alias androiddebugkey -storepass android -keypass android \
  -keyalg RSA -keysize 3072 -validity 10000 \
  -dname 'CN=Kyrex Android Test, O=Kyrex, C=US'
chmod 600 "$HOME/.config/kyrex-signing/chat-test.keystore"
```

Do not run the generation command over an existing key or replace an existing
repository secret without verifying which installed builds use it. Back up the
private keystore through approved private storage and retain its public SHA-256
fingerprint separately. Never add a keystore to Git, logs or downloadable build
artifacts. Base64 encoding is not encryption.

For GitHub CI:

1. Open the repository's Settings > Secrets and variables > Actions. The owner
   must create or deliberately update the repository secret named
   `KYREX_CHAT_ANDROID_KEYSTORE_B64` with the keystore's base64 value. Avoid
   printing it into logs or sharing it through Chat. In WSL, the owner can copy
   directly to the Windows clipboard with:

   ```sh
   base64 -w 0 "$HOME/.config/kyrex-signing/chat-test.keystore" | clip.exe
   ```

   Paste only into that GitHub secret field, save, then clear the clipboard.
   GitHub secret administration is not available through the current Kyrex
   development GitHub connector; do not embed a token in a workflow to automate
   around that limitation.
2. Run **Check integrated Android Chat** from the Actions UI, or build the next
   trusted commit. The signing step checks the keystore and alias. A malformed
   configured key fails the build rather than falling back to a temporary key.
3. Download `Kyrex-Chat-Android-v0.5` only when the run summary confirms retained
   signing. It contains the APK and public fingerprint; checks-only runs publish
   reports but no APK artifact. Future runs using that same key can update
   compatible installations. A previously downloaded temporary-key artifact
   remains incompatible even after the repository secret is configured.
4. Set Cloud's `KYREX_ANDROID_CERT_SHA256` to the retained build's public
   fingerprint (or the explicit supported fingerprint list) for TWA domain
   verification. This is separate from Android install compatibility. Do not
   disable domain verification to hide a toolbar or fix an install failure.

Without the signing secret, CI still compiles, tests, lints and assembles on its
runner so fork PRs and development checks remain useful. Those temporary-key
APKs stay on the runner and are not published as installable artifacts.

Reference: [Android app signing and update compatibility](https://developer.android.com/studio/publish/app-signing).
