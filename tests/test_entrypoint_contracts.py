import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_root_entrypoint_targets_legacy_app_main():
    entrypoint = _read(ROOT / "rumi_ai" / "__main__.py")
    assert "_LEGACY_ROOT" in entrypoint
    assert "from tobkiri_runtime.app import main" in entrypoint
    assert 'if __name__ == "__main__":' in entrypoint


def test_version_contract_matches_package_version():
    init_text = _read(ROOT / "rumi_ai" / "__init__.py")
    pyproject_text = _read(ROOT / "tobkiri_runtime" / "pyproject.toml")

    init_match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    pyproject_match = re.search(
        r'^\s*version\s*=\s*"([^"]+)"', pyproject_text, re.MULTILINE
    )

    assert init_match, "rumi_ai.__version__ not found"
    assert pyproject_match, "project.version in pyproject.toml not found"
    assert init_match.group(1) == pyproject_match.group(1)


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
