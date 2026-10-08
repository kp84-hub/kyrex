"""Regression checks use newly generated, inert canaries, never real credentials."""
import importlib.util
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("paths_guard", ROOT / "scripts/check_sensitive_paths.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class CredentialPaths(unittest.TestCase):
    def test_sensitive_names_and_safe_examples(self):
        for name in [".env", "nested/.env.production", "automation/jev-router.env",
                     "foo.env.local", "signing.p8", "signing.jks", "nested/id_ed25519",
                     "client_secret_123.json", "serviceAccount-test.json", "cookies.txt",
                     "tokens.json", "browser-host/profiles/alice/state.json",
                     "kyrex-chat/data/conversation.json", "state.sqlite3-wal"]:
            with self.subTest(name=name):
                self.assertIsNotNone(guard.sensitive_reason(name))
        for name in [".env.example", ".env.production.template", "automation/jev-router.env.example",
                     "ssh_key.pub", "server.crt", "tests/privacy.py", "package-lock.json"]:
            with self.subTest(name=name):
                self.assertIsNone(guard.sensitive_reason(name))

    def test_forced_add_rename_and_staged_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            def check():
                return subprocess.run([sys.executable, str(ROOT / "scripts/check_sensitive_paths.py"), "--staged"],
                                      cwd=repo, capture_output=True, text=True)
            git("init", "-q")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "user.name", "Credential guard test")
            git("config", "commit.gpgsign", "false")
            git("config", "core.hooksPath", str(repo / "no-hooks"))
            (repo / ".gitignore").write_text(".env\n")
            (repo / ".env").write_text("CANARY=private fixture\n")
            git("add", ".gitignore")
            git("add", "-f", ".env")
            result = check()
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("private fixture", result.stderr)
            git("rm", "--cached", ".env")
            (repo / "safe.txt").write_text("placeholder\n")
            git("add", "safe.txt")
            git("commit", "-qm", "Initial inert fixture")
            git("mv", "safe.txt", "id_rsa")
            self.assertEqual(check().returncode, 1)
            git("rm", "-f", "id_rsa")
            self.assertEqual(check().returncode, 0)


@unittest.skipUnless(shutil.which("gitleaks"), "Install pinned Gitleaks to run content regression checks")
class CredentialContent(unittest.TestCase):
    def test_new_tokens_are_blocked_in_tests_and_examples_with_redacted_output(self):
        # The fixture is random and generated at runtime so the tests themselves
        # do not teach the scanner to ignore another credential-shaped literal.
        token = "gh" + "p_" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(36))
        generic_canary = secrets.token_hex(24)
        for relative, canary in [("kyrex-chat/tests/connections.test.mjs", token),
                                 ("kyrex-chat/tests/connections.test.mjs", generic_canary),
                                 (".env.example", token)]:
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory)
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'api_key = "{canary}" // gitleaks:allow\n')
                self.assertIsNone(guard.sensitive_reason(relative))
                subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
                subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
                result = subprocess.run(["gitleaks", "git", "--pre-commit", "--staged", "--redact=100",
                                         "--ignore-gitleaks-allow", "--no-banner", "--config", str(ROOT / ".gitleaks.toml")],
                                        cwd=repo, capture_output=True, text=True)
                if canary in result.stdout + result.stderr:
                    self.fail("Scanner output did not redact the generated canary")
                self.assertEqual(result.returncode, 1, "New credential must fail even inside an allowlisted test path")

    def test_private_key_content_in_an_ordinary_file_is_blocked(self):
        import base64
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            encoded = base64.b64encode(os.urandom(192)).decode()
            (repo / "notes.txt").write_text("-----BEGIN " + "RSA PRIVATE KEY-----\n" + encoded + "\n-----END RSA PRIVATE KEY-----\n")
            result = subprocess.run(["gitleaks", "dir", "--config", str(ROOT / ".gitleaks.toml"),
                                     "--redact=100", "--ignore-gitleaks-allow", "--no-banner", str(repo)],
                                    capture_output=True, text=True)
            if encoded in result.stdout + result.stderr:
                self.fail("Scanner output did not redact the generated key canary")
            self.assertEqual(result.returncode, 1, "Key contents must fail regardless of filename")

    def test_placeholder_is_allowed_but_exception_does_not_follow_value_to_new_path(self):
        import tomllib
        config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
        value_regex = config["allowlists"][0]["regexes"][0]
        # Build the already-reviewed sequential fixture from its exact regex.
        value = value_regex[1:-1].replace("\\-", "-")
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".env.example").write_text("API_KEY=<set-in-deployment>\n")
            result = subprocess.run(["gitleaks", "dir", "--config", str(ROOT / ".gitleaks.toml"),
                                     "--redact=100", "--no-banner", str(repo)], capture_output=True)
            self.assertEqual(result.returncode, 0)
            (repo / "another_test.go").write_text(f'const apiKey = "{value}"\n')
            result = subprocess.run(["gitleaks", "dir", "--config", str(ROOT / ".gitleaks.toml"),
                                     "--redact=100", "--no-banner", str(repo)], capture_output=True)
            self.assertEqual(result.returncode, 1, "Fixture exceptions must require both the value and its reviewed path")


if __name__ == "__main__":
    unittest.main()
