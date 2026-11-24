"""The packaged Shell entrypoint must load before it configures import roots."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_defaultspack_entrypoint_imports_in_isolated_python(tmp_path: Path) -> None:
    entrypoint = (
        Path(__file__).resolve().parents[1]
        / "ecosystem" / "defaultspack" / "defaultspack" / "desktop_app.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1], run_name='import_probe')",
            str(entrypoint),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_handover_import_works_in_isolated_desktop_script_context(
    tmp_path: Path,
) -> None:
    """Execute the real main import without running server or migration setup."""
    entrypoint = (
        Path(__file__).resolve().parents[1]
        / "ecosystem" / "defaultspack" / "defaultspack" / "desktop_app.py"
    )
    code = """
import ast
import runpy
import sys
from pathlib import Path

entrypoint = Path(sys.argv[1])
namespace = runpy.run_path(str(entrypoint), run_name="desktop_script_probe")
assert not namespace["__package__"]
namespace["_ensure_import_path"]()
tree = ast.parse(entrypoint.read_text(encoding="utf-8"))
main = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
            and node.name == "main")
imports = [node for node in ast.walk(main) if isinstance(node, ast.ImportFrom)
           and any(alias.name == "DevelopmentHostHandover"
                   for alias in node.names)]
assert len(imports) == 1
# Run the production import in the same package-less namespace used by the
# Developer script runner. Keep main's state/server setup entirely uncalled.
statement = ast.Module(body=imports, type_ignores=[])
exec(compile(statement, str(entrypoint), "exec"), namespace)
assert namespace["DevelopmentHostHandover"].__module__ == (
    "ecosystem.defaultspack.defaultspack.development_host_handover"
)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(entrypoint)],
        cwd=tmp_path,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
