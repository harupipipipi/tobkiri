"""Merged regressions must import the selected saved-turn implementation owner."""

import ast
from pathlib import Path


def test_runtime_tests_do_not_import_removed_saved_turn_implementation() -> None:
    root = Path(__file__).resolve().parents[1] / "tobkiri_runtime" / "tests"
    stale = []
    for path in root.rglob("test_*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                if node.module == "ecosystem.defaultspack.runtime" and any(
                    alias.name == "saved_conversation" for alias in node.names
                ):
                    stale.append(f"{path.name}:{node.lineno}")
                elif node.module == "ecosystem.defaultspack.runtime.saved_conversation":
                    stale.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Import) and any(
                alias.name == "ecosystem.defaultspack.runtime.saved_conversation"
                for alias in node.names
            ):
                stale.append(f"{path.name}:{node.lineno}")
    assert not stale, "Removed saved-turn owner imports: " + ", ".join(stale)
