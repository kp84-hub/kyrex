# Kyrex Health Android companion

Read-only Samsung Health → Health Connect → Kyrex bridge. Pair with a one-use
code from Kyrex Connections, grant the chosen read permissions, and manually
sync the last seven days. See [setup and data behavior](../docs/connectors/fitness.md).

Open this directory in Android Studio, using JDK 17 and Android SDK 36. Build
with Gradle 8.11.1 (`gradle :app:assembleDebug`). No server credentials are compiled
into the app; the user supplies their own HTTPS Kyrex origin during pairing.
