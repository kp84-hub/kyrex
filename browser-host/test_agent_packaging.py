"""test_agent_packaging.py — packaging regression for the Phase-2 agent image.

Proves the DEPLOYED cloud-agent container (what ``docker-compose.cloud.yml``
builds) actually runs the manual/automation flock:

  1. the cloud compose builds the expected agent Dockerfile;
  2. that Dockerfile copies ``agent.py``, ``host_allowlist.py`` AND
     ``manual_mode.py`` into import-compatible container paths;
  3. ``WORKDIR /host`` + same-directory copies make ``import manual_mode``
     resolvable inside the container (agent.py does a bare
     ``import manual_mode`` with ``sys.path`` anchored at its own dir);
  4. the check REPRODUCIBLY FAILS against the pre-fix Dockerfile
     (``git show HEAD:browser-host/Dockerfile``), which shipped agent.py
     without manual_mode.py — so this test is smoke-proof, not decorative;
  5. the viewer image stays SEPARATE: Dockerfile.viewer must not gain agent
     code, and the viewer stack must not build the agent image.

Read-only vs. the daemon: parses files, never contacts Docker.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

BROWSER_HOST_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = BROWSER_HOST_DIR.parent

COPY_RE = re.compile(
    r"^\s*COPY\s+(?P<src>.+?)\s+(?P<dest>/\S+)\s*$", re.MULTILINE)


def _read(rel: str, repo: pathlib.Path = REPO_ROOT) -> str:
    return (repo / rel).read_text()


def _cloud_compose_service() -> tuple[pathlib.Path, str, str]:
    """Resolve the dockerfile the cloud agent stack ACTUALLY builds.

    Returns (dockerfile path, build context dir, container workdir).
    Compose build context is ``..`` relative to the compose file's dir.
    """
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover
        pytest.skip("PyYAML required for compose inspection")
    doc = yaml.safe_load((BROWSER_HOST_DIR /
                          "docker-compose.cloud.yml").read_text())
    build = doc["services"]["agent"]["build"]
    ctx = (BROWSER_HOST_DIR / build["context"]).resolve()
    dockerfile = ctx / build["dockerfile"]
    assert dockerfile.is_file(), f"agent Dockerfile missing: {dockerfile}"
    return dockerfile, ctx, doc["services"]["agent"].get("working_dir", "/")


def _coped_files(dockerfile_text: str) -> dict:
    """Map COPY destination directory -> set of source basenames."""
    copies: dict[str, set[str]] = {}
    for m in COPY_RE.finditer(dockerfile_text):
        dest = pathlib.PurePosixPath(m.group("dest"))
        # A COPY to a file inside /host still lands that file in /host's
        # import directory; key by the destination's parent (or the dest
        # itself when it is a directory-style target).
        dest_dir = dest.parent if dest.suffix and dest.name else dest
        src = m.group("src").split()[-1]
        copies.setdefault(str(dest_dir).rstrip("/") or "/", set()).add(
            pathlib.PurePosixPath(src).name)
    return copies


def _workdir(dockerfile_text: str) -> str:
    matches = re.findall(r"^\s*WORKDIR\s+(\S+)\s*$",
                         dockerfile_text, re.MULTILINE)
    return matches[-1] if matches else "/"


def _imports_ok(dockerfile_text: str) -> None:
    """Fail unless the image lets /host/agent.py `import manual_mode`."""
    workdir = _workdir(dockerfile_text)
    copies = _coped_files(dockerfile_text)
    host = copies.get(workdir, set())
    for module in ("agent.py", "host_allowlist.py", "manual_mode.py"):
        assert module in host, (
            f"agent image does not COPY {module} into {workdir}; "
            "the manual/automation flock would silently disable itself "
            f"(`import {module[:-3]}` fails at import time)")


def _workdir(dockerfile_text: str) -> str:
    matches = re.findall(r"^\s*WORKDIR\s+(\S+)\s*$",
                         dockerfile_text, re.MULTILINE)
    return matches[-1] if matches else "/"


# ── 1. the cloud stack builds the agent Dockerfile ────────────────────

def test_cloud_compose_builds_the_agent_dockerfile():
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover
        pytest.skip("PyYAML required for compose inspection")
    doc = yaml.safe_load((BROWSER_HOST_DIR /
                          "docker-compose.cloud.yml").read_text())
    build = doc["services"]["agent"]["build"]
    assert build["dockerfile"] == "browser-host/Dockerfile"
    assert dockerfile_from(build) == BROWSER_HOST_DIR / "Dockerfile"


def dockerfile_from(build: dict) -> pathlib.Path:
    ctx = (BROWSER_HOST_DIR / build["context"]).resolve()
    return ctx / build["dockerfile"]


# ── 2. the deployed image ships the whole /host import set ────────────

def test_agent_image_copies_the_import_cohort_into_the_workdir():
    text = _read("browser-host/Dockerfile")
    assert _workdir(text).rstrip("/") == "/host", (
        "agent image must keep its code in one import directory (/host)")
    _imports_ok(text)


# ── 3. import compatibility: bare import resolves from /host ──────────

def test_agent_py_imports_manual_mode_from_its_own_directory():
    source = _read("browser-host/agent.py")
    assert re.search(r"^\s*import manual_mode\b", source, re.MULTILINE), (
        "agent.py must depend on a same-directory manual_mode import")
    assert 'sys.path.insert' in source, (
        "agent.py anchors imports at its own directory — so the Dockerfile "
        "must put manual_mode.py in the same directory as agent.py")


# ── 4. the test fails against the PRE-FIX Dockerfile ──────────────────

def test_check_reproduces_the_prefix_defect():
    """The detector must fail on the pre-fix image (and only there)."""
    head = subprocess.run(
        ["git", "show", "HEAD:browser-host/Dockerfile"],
        capture_output=True, text=True, cwd=REPO_ROOT)
    if head.returncode != 0 and "not a git repository" in (head.stderr or ""):
        pytest.skip("pre-fix Dockerfile snapshot unavailable: this checkout "
                    "is a bare export (no git metadata) — the defect-replay "
                    "comparison requires the repository history")
    assert head.returncode == 0, "cannot read the pre-fix Dockerfile"
    with pytest.raises(AssertionError, match="manual_mode"):
        _imports_ok(head.stdout)
    # ...while the current (fixed) file passes the same check.
    _imports_ok(_read("browser-host/Dockerfile"))


# ── 5. the viewer image stays separate and untouched ──────────────────

def test_viewer_image_is_separate_and_gains_no_agent_code():
    text = _read("browser-host/Dockerfile.viewer")
    copies = _coped_files(text)
    all_src = {s for group in copies.values() for s in group}
    assert "manual_mode.py" in all_src
    assert "agent.py" not in all_src, (
        "the viewer image must not run agent code — it is a separate image")
    entry = re.search(r'^ENTRYPOINT\s+(\[.*\]|\S.*)$',
                      text, re.MULTILINE).group(1)
    assert "viewer_ctl.py" in entry and "agent.py" not in entry


def test_viewer_compose_never_builds_the_agent_image():
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover
        pytest.skip("PyYAML required for compose inspection")
    doc = yaml.safe_load(
        (BROWSER_HOST_DIR / "docker-compose.viewer.yml").read_text())
    build = doc["services"]["viewer"]["build"]
    assert build["dockerfile"] == "browser-host/Dockerfile.viewer"
