"""Regression checks for Cloud's installed Kyrex engine dependency."""

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CHAT_SERVICE = REPO_ROOT / "kyrex-cloud" / "web" / "backend" / "chat_service.py"
DOCKERFILES = (
    REPO_ROOT / "kyrex-cloud" / "Dockerfile",
    REPO_ROOT / "kyrex-cloud" / "web" / "Dockerfile",
)


def _dockerfile_install_command(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    commands = []
    current = ""
    for line in lines:
        stripped = line.strip()
        if current:
            current += " " + stripped.removesuffix("\\").strip()
            if not stripped.endswith("\\"):
                commands.append(current)
                current = ""
        elif stripped.startswith("RUN pip install"):
            current = stripped.removesuffix("\\").strip()
            if not stripped.endswith("\\"):
                commands.append(current)
                current = ""
    return "\n".join(commands)


def test_cloud_images_install_local_engine_package():
    for dockerfile in DOCKERFILES:
        install_command = _dockerfile_install_command(dockerfile)
        assert "./kyrex_engine" in install_command, dockerfile


def test_chat_service_does_not_add_engine_source_tree_to_sys_path():
    tree = ast.parse(CHAT_SERVICE.read_text(encoding="utf-8"))
    engine_path_insertions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "insert"
        and any(
            isinstance(descendant, ast.Name) and descendant.id == "ENGINE_DIR"
            for descendant in ast.walk(node)
        )
    ]
    assert not engine_path_insertions
