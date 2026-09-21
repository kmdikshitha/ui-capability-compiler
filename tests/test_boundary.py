"""The import boundary.

Ten lines that mechanically prove the central claim of the project instead of
asserting it in prose: the deterministic replay path cannot reach a model.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPLAY = Path(__file__).resolve().parents[1] / "src" / "replay"
FORBIDDEN = ("anthropic", "src.agent")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("path", sorted(REPLAY.rglob("*.py")), ids=lambda p: p.name)
def test_replay_never_imports_a_model(path: Path) -> None:
    for module in _imports(path):
        for banned in FORBIDDEN:
            assert not (module == banned or module.startswith(banned + ".")), (
                f"{path.name} imports {module!r}; replay is the no-model path"
            )


def test_replay_transitively_clean() -> None:
    """Also check what replay's own dependencies drag in."""
    seen: set[str] = set()
    root = Path(__file__).resolve().parents[1]

    def walk(module: str) -> None:
        if module in seen or not module.startswith("src."):
            return
        seen.add(module)
        path = root / (module.replace(".", "/") + ".py")
        if not path.exists():
            return
        for imported in _imports(path):
            for banned in FORBIDDEN:
                assert not imported.startswith(banned), (
                    f"{module} reaches {imported!r} transitively from replay")
            walk(imported)

    for entry in REPLAY.rglob("*.py"):
        for imported in _imports(entry):
            walk(imported)
