import re
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_root_entrypoint_targets_canonical_authority_bound_main():
    entrypoint = _read(ROOT / "tobkiri" / "__main__.py")
    assert "from .runtime import main" in entrypoint
    assert "sys.path" not in entrypoint
    assert 'if __name__ == "__main__":' in entrypoint
    assert not (ROOT / "rumi_ai").exists()


def test_version_contract_matches_package_version():
    pyproject_text = _read(ROOT / "tobkiri_runtime" / "pyproject.toml")
    result = subprocess.run(
        [sys.executable, "-B", "-c", "import tobkiri; print(tobkiri.__version__)"],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=20,
    )
    pyproject_match = re.search(
        r'^\s*version\s*=\s*"([^"]+)"', pyproject_text, re.MULTILINE
    )

    assert pyproject_match, "project.version in pyproject.toml not found"
    assert result.stdout.strip() == pyproject_match.group(1)


def test_repository_package_keeps_canonical_cli_modules_importable():
    result = subprocess.run(
        [sys.executable, "-B", "-c",
         "import json, tobkiri.cli, tobkiri.runtime; "
         "print(json.dumps([tobkiri.cli.__file__, tobkiri.runtime.__file__]))"],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=20,
    )
    paths = [Path(value).resolve() for value in json.loads(result.stdout)]
    assert paths == [ROOT / "tobkiri_runtime/tobkiri/cli.py",
                     ROOT / "tobkiri_runtime/tobkiri/runtime.py"]


def test_checkout_and_installed_layouts_reject_unbound_startup(tmp_path):
    for cwd, module, runtime_path in (
        (ROOT, "tobkiri", ""),
        (tmp_path, "tobkiri", str(ROOT / "tobkiri_runtime")),
        (tmp_path, "rumi_ai", str(ROOT / "tobkiri_runtime")),
    ):
        environment = {**os.environ, "PYTHONPATH": runtime_path,
                       "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            [sys.executable, "-B", "-m", module], cwd=cwd, env=environment,
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode != 0
        assert "Launcher-injected Pack v4 activation snapshot" in result.stderr
        assert "Traceback" not in result.stderr


def test_control_panel_bundle_uses_v3_startup_profile_contract():
    web_root = ROOT / "tobkiri_runtime" / "core_runtime" / "core_pack" / "core_control_panel" / "web"
    scripts = list((web_root / "assets").glob("*.js"))

    assert scripts, "control panel web bundle is missing"

    bundle_text = "\n".join(_read(script) for script in scripts)
    assert "base_pack" in bundle_text
    assert "standard_pack_id" not in bundle_text


def test_pack_architecture_entrypoint_targets_canonical_runtime():
    entrypoint = _read(ROOT / "scripts" / "quality" / "scan_pack_architecture.py")

    assert '"tobkiri_runtime"' in entrypoint
    assert '"rumi_ai_1_10"' not in entrypoint


def test_just_windows_shell_supports_existing_command_chains():
    justfile = _read(ROOT / "justfile")

    assert 'set windows-shell := ["cmd.exe", "/C"]' in justfile
    assert "powershell.exe" not in justfile
