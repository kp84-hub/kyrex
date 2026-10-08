#!/usr/bin/env python3
"""Reject sensitive tracked/staged paths, including files added with git add -f.

Only filenames are read or reported. Gitleaks separately checks file contents.
"""
import argparse
import fnmatch
import json
from pathlib import PurePosixPath
import subprocess
import sys


def sensitive_reason(path: str) -> str | None:
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1]
    env = name == ".env" or name.startswith(".env.") or name.endswith(".env") or ".env." in name
    if env and not name.endswith((".example", ".sample", ".template")):
        return "live environment file"
    if name.endswith((".pem", ".key", ".p8", ".p12", ".pfx", ".jks", ".keystore")):
        return "private key or signing container"
    if name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        return "SSH private key"
    patterns = ("credentials.json", "credentials-*.json", "credentials_*.json",
                "client_secret*.json", "service-account*.json", "service_account*.json",
                "serviceaccount*.json", "application_default_credentials.json")
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
        return "credential export"
    if name in {"cookies.json", "cookies.txt", "token.json", "tokens.json"}:
        return "session or token export"
    if name.endswith((".sqlite", ".sqlite3")) or ".sqlite-" in name or ".sqlite3-" in name:
        return "private runtime database"
    if any(part in {".px", ".vael_sessions", ".px_sessions", ".aws"} for part in parts):
        return "private runtime or credential directory"
    if name in {".px_history", ".vael_history", "viewer-vnc-pass"}:
        return "private session history or password"
    normalized = "/".join(parts)
    if normalized.startswith(("browser-host/profiles/", "kyrex-chat/data/")):
        return "private runtime data"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="check the proposed index, including forced additions")
    args = parser.parse_args()
    # Inspect the complete proposed tree rather than the working copy. Deleted
    # files disappear; renames into sensitive paths and already-tracked files fail.
    try:
        output = subprocess.check_output(["git", "ls-files", "--cached", "-z"])
    except (OSError, subprocess.CalledProcessError):
        print("Cannot inspect the Git index; credential path check failed.", file=sys.stderr)
        return 2
    failures = [(path, sensitive_reason(path)) for path in output.decode("utf-8", "surrogateescape").split("\0") if path]
    failures = [(path, reason) for path, reason in failures if reason]
    for path, reason in failures:
        print(f"Blocked {json.dumps(path)}: {reason}", file=sys.stderr)
    if failures:
        print("Remove these files from the index and keep credentials in environment variables or a secret store.", file=sys.stderr)
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
