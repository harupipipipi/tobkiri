"""Tests for the rootless formal Git/source authority boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "packaging_toolchain_identity.py"
_SPEC = importlib.util.spec_from_file_location("packaging_identity_test", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load packaging identity script: {_SCRIPT}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

ToolIdentityError = _MODULE.ToolIdentityError


def _executable(path: Path, payload: bytes = b"#!/bin/sh\nexit 0\n") -> Path:
    path.write_bytes(payload)
    path.chmod(0o500)
    return path


def _git() -> _MODULE.ToolIdentity:
    return _MODULE.bind_git()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", root], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", root, "config", "user.name", "Tobkiri Test"],
        check=True,
    )
    subprocess.run(
        ["/usr/bin/git", "-C", root, "config", "user.email", "test@example.invalid"],
        check=True,
    )
    files = {
        ".github/scripts/build_sealed_python_environment.py": b"# builder\n",
        ".github/scripts/prepare_tauri_resources.py": b"# resources\n",
        ".github/scripts/sealed_python_sources/launcher.py": b"# launcher\n",
        "tobkiri_runtime/packaged_defaultspack_source_manifest.v1.json": b"{}\n",
        "tobkiri_runtime/module.py": b"VALUE = 1\n",
        "tobkiri_runtime/python-runtime/forbidden": b"must not copy\n",
        "outside.txt": b"must not copy\n",
    }
    for relative, payload in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    subprocess.run(["/usr/bin/git", "-C", root, "add", "."], check=True)
    subprocess.run(["/usr/bin/git", "-C", root, "commit", "-qm", "fixture"], check=True)
    commit = subprocess.check_output(
        ["/usr/bin/git", "-C", root, "rev-parse", "HEAD"], text=True
    ).strip()
    return root, commit


def test_production_module_contains_no_privileged_or_dynamic_root_surface() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    forbidden = (
        "ROOT_BROKER",
        "ROOT_INSTALLATION",
        "internal-root",
        '"/usr/bin/sudo"',
        "prepare_macos_installation",
        "cleanup_macos_installation",
        "exec(compile",
    )
    for marker in forbidden:
        assert marker not in source


def test_bind_toolchain_requires_exact_regular_nonwritable_executables(
    tmp_path: Path,
) -> None:
    python = _executable(tmp_path / "python")
    git = _executable(tmp_path / "git")
    identities = _MODULE.bind_toolchain(python=str(python), git=str(git))
    assert (
        identities["python"].sha256 == hashlib.sha256(python.read_bytes()).hexdigest()
    )
    git.chmod(0o700)
    with pytest.raises(ToolIdentityError, match="immutable"):
        _MODULE.bind_toolchain(python=str(python), git=str(git))


def test_environment_writer_rejects_multiline_and_symlink(
    tmp_path: Path,
) -> None:
    output = tmp_path / "environment"
    with pytest.raises(ToolIdentityError, match="unsafe environment"):
        _MODULE.write_environment_file(output, "KEY=value\nINJECT")
    target = tmp_path / "target"
    target.write_text("sentinel", encoding="utf-8")
    output.symlink_to(target)
    with pytest.raises(ToolIdentityError, match="unsafe environment"):
        _MODULE.write_environment_file(output, "KEY=value\n")
    assert target.read_text(encoding="utf-8") == "sentinel"


def test_git_environment_disables_all_configuration_and_helpers(tmp_path: Path) -> None:
    environment = _MODULE._git_environment(tmp_path)
    assert environment["GIT_CONFIG"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_EXEC_PATH"] == str(_MODULE.ISOLATED_GIT_EXEC_PATH)
    assert environment["HOME"] == str(_MODULE.ISOLATED_GIT_EXEC_PATH)
    assert environment["PATH"] == "/usr/bin:/bin"


def test_formal_git_rejects_config_filter_and_dirty_bytes(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path)
    marker = tmp_path / "marker"
    filter_script = tmp_path / "filter.sh"
    filter_script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
    filter_script.chmod(0o700)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            root,
            "config",
            "filter.review.clean",
            str(filter_script),
        ],
        check=True,
    )
    (root / ".gitattributes").write_text("* filter=review\n", encoding="utf-8")
    # Neither local config nor worktree attributes are an authority for plumbing reads.
    with pytest.raises(ToolIdentityError, match="untracked"):
        _MODULE._verify_clean_checkout(_git(), root, commit)
    assert not marker.exists()
    (root / ".gitattributes").unlink()
    path = root / "tobkiri_runtime/module.py"
    path.write_bytes(b"VALUE = 2\n")
    with pytest.raises(ToolIdentityError, match="bytes changed"):
        _MODULE._verify_clean_checkout(_git(), root, commit)
    assert not marker.exists()


def test_snapshot_is_exact_committed_private_source(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path)
    destination = tmp_path / "snapshot"
    tree, release, inventory = _MODULE.snapshot_committed_source(
        _git(), root, commit, destination
    )
    assert len(tree) == 40
    assert len(release) == 64
    assert len(inventory) == 64
    inventory_path = destination / _MODULE.SOURCE_SNAPSHOT_MANIFEST
    assert hashlib.sha256(inventory_path.read_bytes()).hexdigest() == inventory
    assert (destination / "tobkiri_runtime/module.py").read_bytes() == b"VALUE = 1\n"
    assert not (destination / "outside.txt").exists()
    assert not (destination / "tobkiri_runtime/python-runtime").exists()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o500
    for path in destination.rglob("*"):
        mode = stat.S_IMODE(path.lstat().st_mode)
        assert mode in {0o400, 0o500}


def test_snapshot_uses_committed_blob_after_checkout_swap(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path)
    path = root / "tobkiri_runtime/module.py"
    path.write_bytes(b"ATTACKER = True\n")
    destination = tmp_path / "snapshot"
    _MODULE.snapshot_committed_source(_git(), root, commit, destination)
    assert (destination / "tobkiri_runtime/module.py").read_bytes() == b"VALUE = 1\n"


def test_snapshot_rejects_existing_destination_and_lowercase_commit(
    tmp_path: Path,
) -> None:
    root, commit = _repository(tmp_path)
    destination = tmp_path / "snapshot"
    destination.mkdir()
    with pytest.raises(ToolIdentityError, match="new and absolute"):
        _MODULE.snapshot_committed_source(_git(), root, commit, destination)
    with pytest.raises(ToolIdentityError, match="full lowercase"):
        _MODULE.snapshot_committed_source(
            _git(), root, commit.upper(), tmp_path / "new"
        )


def test_snapshot_construction_failure_leaves_private_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, commit = _repository(tmp_path)
    destination = tmp_path / "snapshot"
    original = _MODULE._git_output

    def fail_after_creation(
        git: _MODULE.ToolIdentity, repository: Path, *arguments: str
    ) -> bytes:
        if arguments[:2] == ("ls-tree", "-r"):
            raise ToolIdentityError("injected object read failure")
        return original(git, repository, *arguments)

    monkeypatch.setattr(_MODULE, "_git_output", fail_after_creation)
    with pytest.raises(ToolIdentityError, match="injected"):
        _MODULE.snapshot_committed_source(_git(), root, commit, destination)
    assert destination.is_dir()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700


def test_release_digest_binds_commit_tree_and_manifest(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path)
    destination = tmp_path / "snapshot"
    tree, release, inventory = _MODULE.snapshot_committed_source(
        _git(), root, commit, destination
    )
    manifest = destination.joinpath(*_MODULE.SOURCE_MANIFEST.parts).read_bytes()
    framed = _MODULE._canonical_json(
        {
            "schema": _MODULE.SOURCE_SNAPSHOT_SCHEMA,
            "source_commit": commit,
            "source_tree": tree,
            "source_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "source_inventory_sha256": inventory,
        }
    )
    assert release == hashlib.sha256(framed).hexdigest()


def test_append_git_environment_requires_complete_source_and_python_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python_root = tmp_path / "python"
    source_root = tmp_path / "source"
    python_root.mkdir()
    source_root.mkdir()
    python = _executable(python_root / "python")
    output = tmp_path / "binding"
    output.write_text(
        "TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256=" + "a" * 64 + "\n"
        f"TOBKIRI_PACKAGING_PYTHON={python}\n"
        "TOBKIRI_PACKAGING_PYTHON_SHA256="
        + hashlib.sha256(python.read_bytes()).hexdigest()
        + "\n"
        f"TOBKIRI_PACKAGING_PYTHON_SNAPSHOT={python_root}\n"
        "TOBKIRI_PACKAGING_PYTHON_INVENTORY_SHA256=" + "a" * 64 + "\n"
        f"TOBKIRI_PACKAGING_SOURCE_SNAPSHOT={source_root}\n"
        "TOBKIRI_PACKAGING_SOURCE_TREE=" + "b" * 40 + "\n"
        "TOBKIRI_PACKAGING_SOURCE_INVENTORY_SHA256=" + "e" * 64 + "\n"
        "TOBKIRI_PACKAGING_RELEASE_DIGEST=" + "c" * 64 + "\n",
        encoding="utf-8",
    )
    output.chmod(0o600)
    git = _MODULE.ToolIdentity(Path("/formal/git"), "d" * 64)
    monkeypatch.setattr(_MODULE, "bind_git", lambda: git)
    _MODULE.append_git_environment_file(output)
    assert output.read_text(encoding="utf-8").endswith(
        "TOBKIRI_PACKAGING_GIT=/formal/git\n"
        "TOBKIRI_PACKAGING_GIT_SHA256=" + "d" * 64 + "\n"
    )


@pytest.mark.parametrize("workflow_name", ["desktop-installers.yml", "release.yml"])
def test_workflow_uses_snapshot_builder_and_has_no_sudo(workflow_name: str) -> None:
    workflow = _SCRIPT.parents[1] / "workflows" / workflow_name
    source = workflow.read_text(encoding="utf-8")
    assert "--snapshot-source" in source
    assert (
        '"$source_snapshot/.github/scripts/build_sealed_python_environment.py"'
        in source
    )
    assert "TOBKIRI_PACKAGING_PYTHON_SNAPSHOT" in source
    assert "/usr/bin/sudo" not in source
    assert "internal-root" not in source
    assert 'cat-file blob "$identity_oid" > "$identity_launcher"' in source
    assert 'hash-object "$identity_launcher"' in source
    assert "formal_identity() {" in source
    assert 'exec 9< "$identity_launcher"' in source
    assert 'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I -B - "$@" <&9' in source
    assert 'IFS= read -r -n 1 <&9' in source
    assert 'exec 9<&-' in source
    assert "exec {identity_fd}" not in source
    assert "/dev/fd" not in source
    assert "eval " not in source
    assert not any(
        line.strip().startswith(("source ", ". "))
        for line in source.splitlines()
    )
    assert source.count('exec 9< "$identity_launcher"') == 1
    assert source.count('PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I -B - "$@" <&9') == 1
    assert source.count("exec 9<&-") == 1
    assert source.count("formal_identity ") == 4
    for invocation in (
        'formal_identity "$pre_git_env"',
        'formal_identity "-"',
        'formal_identity "$source_env"',
        'formal_identity "$TOOLCHAIN_ENV"',
    ):
        assert source.count(invocation) == 1
    assert 'test "$identity_status" -eq 0' in source
    assert 'test -s "$required_output"' in source
    assert 'test -s "$identity_launcher"' in source
    assert "/usr/bin/python3 -B .github/scripts/packaging_toolchain_identity.py" not in source


def test_fixed_identity_fd_reopens_for_each_bash_3_2_invocation(
    tmp_path: Path,
) -> None:
    """Bash 3.2 reopens FD 9 for stdin scripts and proves every postcondition."""
    launcher = tmp_path / "identity launcher.py"
    fake_python = _executable(tmp_path / "non-consuming-python")
    first_output = tmp_path / "first env output"
    second_output = tmp_path / "second env output"
    missing_output = tmp_path / "missing env output"
    launcher.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "arguments = sys.argv[1:]\n"
        "if '--no-output' in arguments:\n"
        "    raise SystemExit(0)\n"
        "output = Path(arguments[arguments.index('--env-output') + 1])\n"
        "marker = arguments[arguments.index('--argv-marker') + 1]\n"
        "output.write_text(\n"
        "    f'output={output.name};marker={marker}\\n', encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    script = r"""
set -euo pipefail
identity_launcher="$1"
real_python="$2"
python="$real_python"
first_output="$3"
second_output="$4"
missing_output="$5"
fake_python="$6"
formal_identity() {
  required_output="$1"
  shift
  identity_status=0
  exec 9< "$identity_launcher"
  if "$python" -I -B - "$@" <&9; then
    identity_status=0
  else
    identity_status=$?
  fi
  if test "$identity_status" -eq 0 && IFS= read -r -n 1 <&9; then
    identity_status=1
  fi
  exec 9<&-
  test "$identity_status" -eq 0
  test -s "$required_output"
}
formal_identity "$first_output" --env-output "$first_output" --argv-marker first
formal_identity "$second_output" --env-output "$second_output" --argv-marker second
grep -q 'marker=first' "$first_output"
grep -q 'marker=second' "$second_output"
test "$(cat "$first_output")" != "$(cat "$second_output")"
if formal_identity "$missing_output" --no-output; then
  exit 1
fi
test ! -e "$missing_output"
python="$fake_python"
if formal_identity "$missing_output" --no-output; then
  exit 1
fi
test ! -e "$missing_output"
"""
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            script,
            "fixed-fd-test",
            str(launcher),
            sys.executable,
            str(first_output),
            str(second_output),
            str(missing_output),
            str(fake_python),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_private_fd_launcher_ignores_checkout_path_swap(tmp_path: Path) -> None:
    """A private verified launcher stream is independent of the checkout name."""
    checkout = tmp_path / "identity.py"
    private = tmp_path / "private.py"
    marker = tmp_path / "marker"
    trusted = b"import sys\nsys.stdout.write('trusted')\n"
    checkout.write_bytes(trusted)
    private.write_bytes(checkout.read_bytes())
    private.chmod(0o400)
    checkout.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    with private.open("rb") as stream:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-", "checkout-argument"],
            stdin=stream,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "trusted"
    assert not marker.exists()


def test_tampered_private_launcher_blob_is_rejected(tmp_path: Path) -> None:
    """A changed private launcher no longer matches its trusted Git blob ID."""
    launcher = tmp_path / "identity launcher.py"
    trusted_payload = b"import sys\nsys.stdout.write('trusted')\n"
    launcher.write_bytes(trusted_payload)
    trusted_digest = hashlib.sha1(
        f"blob {len(trusted_payload)}\0".encode() + trusted_payload
    ).hexdigest()
    launcher.write_bytes(b"import sys\nsys.stdout.write('tampered')\n")
    verifier = (
        "import hashlib, pathlib, sys\n"
        "payload = pathlib.Path(sys.argv[1]).read_bytes()\n"
        "framed = f'blob {len(payload)}\\0'.encode() + payload\n"
        "actual = hashlib.sha1(framed).hexdigest()\n"
        "raise SystemExit(0 if actual == sys.argv[2] else 1)\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            verifier,
            str(launcher),
            trusted_digest,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert result.returncode != 0


def test_cli_rejects_retired_privileged_action() -> None:
    result = subprocess.run(
        [sys.executable, "-B", _SCRIPT, "--prepare-macos-installation"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


def test_windows_launcher_propagates_each_pytest_exit_code() -> None:
    """A later launcher pytest cannot overwrite cleanup-suite failure."""
    workflow = _SCRIPT.parents[1] / "workflows" / "test.yml"
    payload = workflow.read_text(encoding="utf-8")
    step = payload[payload.index("  tobkiri-launcher-windows:") :]

    guard = "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
    assert step.count(guard) == 2
    first_pytest = (
        "python -m pytest -q .github/scripts/tests/test_packaging_cleanup.py"
    )
    second_pytest = (
        "python -m pytest -q "
        "tobkiri_launcher/scripts/tests/test_package_presentation_artifact.py"
    )
    assert f"{first_pytest}\n          {guard}" in step
    assert f"{second_pytest}\n          {guard}" in step
