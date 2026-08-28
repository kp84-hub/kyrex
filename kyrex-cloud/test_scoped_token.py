"""scoped_token_for: per-repo credential, fail-closed on every non-match."""
import os, importlib
import git_workflow as g


def _set(val):
    if val is None:
        os.environ.pop("KYREX_SCOPED_TOKENS", None)
    else:
        os.environ["KYREX_SCOPED_TOKENS"] = val
    importlib.reload(g)


def test_no_map_returns_none():
    _set(None)
    assert g.scoped_token_for("https://github.com/x/y") is None


def test_mapped_repo_returns_token():
    _set('{"github.com/x/y": "tok_abc"}')
    assert g.scoped_token_for("https://github.com/x/y") == "tok_abc"


def test_unmapped_repo_is_none():
    _set('{"github.com/x/y": "tok_abc"}')
    assert g.scoped_token_for("https://github.com/other/repo") is None


def test_garbage_url_is_none():
    _set('{"github.com/x/y": "tok_abc"}')
    assert g.scoped_token_for("not-a-url") is None


def test_bad_json_is_none():
    _set("broken json")
    assert g.scoped_token_for("https://github.com/x/y") is None
    _set(None)


def test_empty_token_value_is_none():
    _set('{"github.com/x/y": ""}')
    assert g.scoped_token_for("https://github.com/x/y") is None
    _set(None)
