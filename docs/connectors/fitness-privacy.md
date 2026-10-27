# Kyrex Fitness Privacy Policy

Effective date: October 5, 2026

This policy covers Kyrex's Oura connector, Samsung Health companion, and use of their data by the Workout Bot. Kyrex is an open-source application. The operator of the Kyrex server controls its hosting, configured model providers, backups, and account settings. This policy describes the fitness integration's implemented data flow.

## Data accessed and purpose

With your authorization, Kyrex requests Oura's Daily, Heartrate, and Workout permissions. These allow it to retrieve sleep, activity and readiness summaries, heart-rate measurements, and workout information for requested date ranges. It does not request your Oura email, personal profile, tags, sessions, SpO2, ring configuration, stress, or heart-health permissions.

The Android companion reads Samsung Health-origin steps, exercise sessions, heart rate, and staged sleep from Health Connect, limited to the categories you permit. It excludes other Health Connect sources, including Oura. It does not write health records. Sync uploads a snapshot of the last seven days to the paired Kyrex server. Automatic sync is optional and requires background read permission. Invalid timestamp intervals are skipped, and incomplete coverage is reported.

Fitness data is used to answer your requests about activity, workouts, sleep, and recovery. Missing data is not evidence of a device or permission fault; for example, a watch that is not worn at night may contain no sleep records.

## Processing and sharing

Oura processes its account data and authorization under its own policies. Health Connect and Samsung Health manage device data under their respective policies. The configured Kyrex hosting service processes data uploaded to the server.

When you ask a Workout Bot to analyze fitness data, authorized measurements and summaries may be included in requests to the language-model provider configured for that Bot. That provider's processing and retention policies apply. Review your Bot's provider settings before using sensitive data. Kyrex's fitness tool does not expose OAuth or phone-pairing credentials to the model.

Responses containing health information can become part of your saved Kyrex conversations. Explicitly saved long-term chat memories can be stored in the configured memory service. Conversation history, memories, server backups, and model-provider retention are separate from the fitness-record store.

## Storage and retention

Oura access and refresh tokens are encrypted in the owner-scoped server connector store until replaced or disconnected. Oura measurements are fetched on demand; the fitness connector does not maintain a separate persistent archive of those measurement responses. Information included in conversations may nevertheless remain in conversation history or explicitly saved memories.

Samsung measurements are encrypted in the server fitness store. During uploads, records older than the ninety-day retention window are pruned. Pruning is performed during sync, not by a continuously running deletion timer. The phone's upload credential is encrypted with Android Keystore protection. The integration uses HTTPS for server communications.

These measures do not make storage or transmission risk-free. Operators should keep their encryption keys stable and private and configure appropriate hosting and backup access.

## Your controls

You choose which permissions to grant. Turn automatic sync off in the companion to stop scheduled uploads, or remove Health Connect permissions to stop local health access.

Disconnect Samsung Health in Kyrex Connections to revoke the server pairing and delete retained Samsung measurements from the active fitness store. Forgetting pairing on the phone alone removes local settings and does not delete server records. Disconnect Oura in Kyrex Connections to remove its stored credentials and invalidate pending connection attempts. You can also revoke authorization with the source provider.

Disconnecting does not automatically delete earlier chat messages, explicitly saved memories, copies retained by model providers, source-provider records, or server backups. Manage those separately with the applicable service or server operator.

## Contact and updates

For this personal Kyrex deployment, use the contact email supplied on its Oura application registration to reach the operator about data access or deletion. For software questions, use the [Kyrex repository](https://github.com/kp84-hub/kyrex). Do not post private health measurements or credentials in public issues.

Changes to this policy should be published with a new effective date when the integration's data flow changes.
