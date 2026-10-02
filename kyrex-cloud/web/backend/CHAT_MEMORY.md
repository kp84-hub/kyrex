# Kyrex Chat memory (Firestore)

This is a separate, opt-in store for facts a user explicitly asks Kyrex to
remember across conversations. Existing chat transcripts continue to use the
per-user JSON files; this change does not copy old messages into Firestore.

## Configure the Railway server

Use the **Kyrex-Chat** Firebase project, its default Firestore database in
production mode, and a service account for that project with the Cloud Datastore
User (`roles/datastore.user`) role. Add these Railway **service variables**:

- `KYREX_FIRESTORE_PROJECT_ID=kyrex-chat`: the Kyrex Firebase project ID.
- `KYREX_FIRESTORE_SERVICE_ACCOUNT_JSON`: the complete service-account JSON
  stored as a Railway secret. Never commit it, paste it into a chat, or expose it
  through frontend environment variables.

Both settings are required; the backend refuses credentials whose project ID
does not match. The server client uses IAM and does not rely on Firestore client
Security Rules. No Firebase web app or Hosting setup is needed.

## User behavior

- In any Kyrex Chat conversation: `Remember that I prefer short replies`.
- In any Kyrex Chat conversation: `What do you remember about me?`.
- To remove a fact: `Forget memory <ID>`, using the ID shown in the list.

Only explicit remember commands save facts. At most 24 facts per signed-in user,
500 characters per fact, and 3,500 characters of memory context are fed to an
ordinary or The Overwatcher turn. A Firestore read outage does not stop unrelated
Gmail, Bot, or chat turns. Failed explicit memory operations are surfaced to the
user. Deleting a chat transcript does not delete these user-owned memories;
the explicit forget command deletes individual facts.
