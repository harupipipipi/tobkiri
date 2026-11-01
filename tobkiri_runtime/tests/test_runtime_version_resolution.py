"""Version resolution must also work in Launcher dependency-only environments."""

import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from tobkiri import _version

pytestmark = pytest.mark.contract


def _missing_distribution(name: str) -> str:
    raise PackageNotFoundError(name)


def _source_tree(tmp_path: Path, monkeypatch, content: str | None) -> Path:
    runtime = tmp_path / "sealed-runtime"
    package = runtime / "tobkiri"
    package.mkdir(parents=True)
    monkeypatch.setattr(_version, "__file__", str(package / "_version.py"))
    monkeypatch.setattr(_version, "version", _missing_distribution)
    if content is not None:
        (runtime / "pyproject.toml").write_text(content, encoding="utf-8")
    return runtime


def test_canonical_installed_metadata_takes_precedence(monkeypatch):
    calls = []

    def installed(name: str) -> str:
        calls.append(name)
        return "2.3.4"

    monkeypatch.setattr(_version, "version", installed)
    assert _version.resolve_version() == "2.3.4"
    assert calls == ["tobkiri-runtime"]


def test_legacy_installed_metadata_remains_supported(monkeypatch):
    def installed(name: str) -> str:
        if name == "tobkiri-runtime":
            raise PackageNotFoundError(name)
        assert name == "rumi-ai"
        return "1.9.0"

    monkeypatch.setattr(_version, "version", installed)
    assert _version.resolve_version() == "1.9.0"


def test_source_fallback_uses_own_project_not_working_directory(tmp_path, monkeypatch):
    _source_tree(
        tmp_path, monkeypatch,
        '[project]\nname = "tobkiri-runtime"\nversion = "1.10.0"\n',
    )
    unrelated = tmp_path / "unrelated-pack"
    unrelated.mkdir()
    (unrelated / "pyproject.toml").write_text(
        '[project]\nname = "tobkiri-runtime"\nversion = "99.0.0"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(unrelated)
    assert _version.resolve_version() == "1.10.0"


@pytest.mark.parametrize("content", [
    None,
    "not valid TOML = [",
    '[project]\nname = "another-project"\nversion = "1.0.0"\n',
    '[project]\nname = "tobkiri-runtime"\n',
    '[project]\nname = "tobkiri-runtime"\nversion = 10\n',
    '[project]\nname = "tobkiri-runtime"\nversion = ""\n',
    '[project]\nname = "tobkiri-runtime"\nversion = " 1.0.0"\n',
    '[project]\nname = "tobkiri-runtime"\nversion = "not-a-version"\n',
    '[project]\nname = "tobkiri-runtime"\nversion = "1"\nversion = "2"\n',
])
def test_invalid_source_metadata_fails_closed(tmp_path, monkeypatch, content):
    _source_tree(tmp_path, monkeypatch, content)
    with pytest.raises(RuntimeError, match="version metadata is unavailable or invalid"):
        _version.resolve_version()


def test_metadata_errors_other_than_missing_are_not_hidden(monkeypatch):
    def broken_metadata(name: str) -> str:
        raise PermissionError("cannot read installed metadata")

    monkeypatch.setattr(_version, "version", broken_metadata)
    with pytest.raises(PermissionError):
        _version.resolve_version()


def test_checkout_fallback_matches_installed_project_version(monkeypatch):
    expected = _version.version("tobkiri-runtime")
    monkeypatch.setattr(_version, "version", _missing_distribution)
    assert _version.resolve_version() == expected


def test_native_preview_and_sdk_verify_without_distribution_metadata(tmp_path):
    runtime = Path(__file__).resolve().parents[1]
    script = '''
import importlib.metadata
import runpy
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
installed_version = importlib.metadata.version
def dependency_only(name):
    if name in {"tobkiri-runtime", "rumi-ai"}:
        raise importlib.metadata.PackageNotFoundError(name)
    return installed_version(name)
importlib.metadata.version = dependency_only

import tobkiri
import rumi_ai
assert tobkiri.__version__ == rumi_ai.__version__ == "1.10.0"
from core_runtime.native_pack_onboarding import preview_signed_pack
fixture = root / "tests/fixtures/qa_frontend_input_signed"
pack = fixture / "qa.frontend.input"
key = fixture / "qa.frontend.input.public.pem"
preview = preview_signed_pack(pack, key)
assert preview["pack_id"] == "qa.frontend.input"
assert preview["requested_capabilities"] == []
sys.argv = ["tobkiri_pack.py", "verify", str(pack), "--public-key", str(key),
            "--publisher-id", "publisher.qa.frontend"]
runpy.run_path(str(root / "scripts/tobkiri_pack.py"), run_name="__main__")
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(runtime)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"pack_id": "qa.frontend.input"' in result.stdout
