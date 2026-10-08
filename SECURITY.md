# Credentials and private data

Kyrex is a public repository. Keep API keys, OAuth tokens, private keys, signing
files, browser profiles, cookies, conversations, and personal databases outside
Git. Use deployment secret settings or local environment files. Examples must
contain placeholders only. Do not put real credentials in issues, pull requests,
logs, screenshots, or test fixtures.

Before committing, install the local checks:

```sh
python3 -m pip install pre-commit
pre-commit install
```

The pinned Gitleaks hook installs its Go toolchain through pre-commit. The path
guard checks the proposed Git tree, even if a file was added with `git add -f`.
Gitleaks checks staged changes for credential content and redacts scan output.
Neither check needs a provider key. Keep hooks installed in each new checkout.

GitHub Actions checks filenames, current files, and reachable history on every
pull request and on pushes to main. The scanner retains upstream detection
rules; `.gitleaks.toml` has exact value-and-path exceptions for reviewed inert
fixtures and documentation placeholders. Never exempt entire test directories
or add real credentials to an exception.

Repository owners should enable GitHub secret scanning and push protection in
**Settings → Code security**, and require the **secrets** job for merges. This
PR does not change repository settings. CI runs after a push; installed hooks
can be bypassed. Detection covers known patterns, not every possible secret or
piece of personal information. Chat's outbound privacy filters are a separate
layer and do not protect Git commits.

## If a credential is exposed

1. Revoke or rotate it at the provider immediately, then update deployment secrets.
2. Remove it from tracked files. Do not repeat the value in a report or commit message.
3. Report the affected path and credential type privately to the repository owner.
4. Coordinate any history rewrite with the owner and contributors. Deleting the
   current file does not remove earlier commits, forks, clones, or cached copies.

For a vulnerability, contact the repository owner privately through an existing
trusted channel. Do not disclose sensitive details in a public issue.
