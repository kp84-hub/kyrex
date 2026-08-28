"""End-to-end: approved external write pushes only with a scoped credential."""
import io, sys, os, importlib
import git_workflow as g


def _verdict(host_line):
    sys.stdin = io.StringIO(host_line + "\nAPPROVED\n")
    return g._get_push_verdict()


def test_approved_with_scoped_token_pushes():
    os.environ["KYREX_SCOPED_TOKENS"] = '{"github.com/someone/extrepo": "stub_tok_123"}'
    importlib.reload(g)
    scoped = g.scoped_token_for("https://github.com/someone/extrepo")
    proceed, tok = _verdict(f"APPROVE {scoped}")
    assert proceed is True and tok == "stub_tok_123"


def test_approved_without_credential_refuses():
    os.environ["KYREX_SCOPED_TOKENS"] = "{}"
    importlib.reload(g)
    scoped = g.scoped_token_for("https://github.com/someone/extrepo")
    line = f"APPROVE {scoped}" if scoped else "APPROVE"
    proceed, tok = _verdict(line)
    assert proceed is False and tok is None
    os.environ.pop("KYREX_SCOPED_TOKENS", None)


def test_deny_refuses():
    proceed, tok = _verdict("DENY")
    assert proceed is False and tok is None


def test_own_repo_allow_no_token():
    # ALLOW path (own repo) proceeds with no scoped token
    sys.stdin = io.StringIO("ALLOW\n")
    proceed, tok = g._get_push_verdict()
    assert proceed is True and tok is None
