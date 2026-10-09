# Changelog

## 0.1.26

- Refresh the shared engine for every build and VSIX; verify contents and build on Windows and Linux.
- Native Approve/Reject prompts for supported confirmations; reject unsupported requests and cancel stale decisions on restart.
- Render final-only answers, keep interrupted partial responses, show incomplete/error outcomes and failed tool badges.
- Display engine-reported usage instead of counting stream chunks as tokens.
- Wait for readiness, report startup timeouts and disconnections, replay status on sidebar open and provide Restart engine.
- Apply connection settings together, accept pasted model IDs and provider presets, and save sidebar API keys in SecretStorage.
- Correct Python requirements and explain remote provider context sharing.
