"""Contract tests for the retired direct presentation-packaging caller."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "package_presentation_artifact.py"


def _run_shim(tmp_path: Path, hostile: Path) -> subprocess.CompletedProcess[str]:
    """Run the retired name with hostile ambient inputs and no trusted root."""
    marker = hostile / "imported.marker"
    (hostile / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({os.fspath(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    (hostile / "package_presentation_artifact.py").write_text(
        f"from pathlib import Path; Path({os.fspath(marker)!r}).write_text('shadowed')\n",
        encoding="utf-8",
    )
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.fspath(hostile),
        "PYTHONHOME": os.fspath(hostile / "not-python"),
        "REPO": os.fspath(hostile),
        "RUMI_CORE_DIR": os.fspath(hostile),
    }
    return subprocess.run(
        [sys.executable, "-I", "-B", os.fspath(SCRIPT_PATH), "--anything"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_module_load_and_cli_fail_before_hostile_import_or_cwd_discovery(
    tmp_path: Path,
) -> None:
    """A stale Python caller cannot select a source tree or publish output."""
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    marker = hostile / "imported.marker"
    result = _run_shim(tmp_path, hostile)
    assert result.returncode != 0
    assert "tobkiri-core-package-defaults-v1" in result.stderr
    assert "run_formal_defaults_packaging" in result.stderr
    assert not marker.exists()


def test_shim_is_import_free_and_has_no_snapshot_loader_surface() -> None:
    """The direct caller must not execute pre-verification source code."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=os.fspath(SCRIPT_PATH))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Import)]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    top_level_calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_reject_direct_caller"
    ]
    assert top_level_calls
    for forbidden in (
        "importlib",
        "exec_module",
        "subprocess",
        "source-provenance-file",
        "TOBKIRI_PACKAGING_SOURCE_PROVENANCE_FILE",
        "TOBKIRI_PRESENTATION_RELEASE_ROOT",
        "generator_source_manifest",
        "os.environ",
        "Path(",
        "getcwd",
        "open(",
    ):
        assert forbidden not in source


def test_function_call_paths_are_fail_closed_in_source() -> None:
    """Legacy function and CLI names only call the immediate rejection path."""
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    for name in ("package_artifact", "main"):
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_reject_direct_caller"
            for node in ast.walk(functions[name])
        )
