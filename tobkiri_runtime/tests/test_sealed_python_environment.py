"""Synthetic contract tests for the fixed sealed Python packaging boundary."""

from __future__ import annotations

import ast
import errno
import importlib.util
import io
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest


pytestmark = pytest.mark.contract
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = ROOT / ".github" / "scripts" / "build_sealed_python_environment.py"
BOOTSTRAP_PATH = (
    ROOT / ".github" / "scripts" / "sealed_python_sources" / "tobkiri_sealed" / "bootstrap.py"
)


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "tobkiri_sealed_python_builder_tests",
        BUILDER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {BUILDER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()

_SEALED_TEST_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)


def _clean_sealed_test_environment() -> dict[str, str]:
    """Keep subprocess fixtures free of host loader and Python injection state."""
    return {key: value for key, value in os.environ.items() if key in _SEALED_TEST_ENV_ALLOWLIST}


def _make_test_mutable(path: Path) -> None:
    """Temporarily grant a fixture path write access for tamper tests."""
    path.chmod(path.stat().st_mode | 0o200)


def _fixture_sources(base: Path, target: str) -> tuple[Path, Path, Path]:
    """Create a tiny runtime and venv with the same target layout as release."""
    spec = BUILDER.target_spec(target)
    runtime = base / "runtime-source"
    venv = base / "venv-source"
    if spec.windows:
        runtime_python = runtime / "python.exe"
        stdlib = runtime / "Lib"
        venv_python = venv / "Scripts" / "python.exe"
        site_packages = venv / "Lib" / "site-packages"
        runtime_native = runtime / "DLLs" / "_ssl.pyd"
    else:
        runtime_python = runtime / "bin" / "python3"
        stdlib = runtime / "lib" / "python3.13"
        venv_python = venv / "bin" / "python3"
        site_packages = venv / "lib" / "python3.13" / "site-packages"
        runtime_native = stdlib / "lib-dynload" / "_ssl.so"

    runtime_python.parent.mkdir(parents=True, exist_ok=True)
    runtime_python.write_bytes(b"synthetic native CPython executable\n")
    runtime_python.chmod(0o755)
    (stdlib / "encodings").mkdir(parents=True)
    (stdlib / "os.py").write_text("synthetic stdlib\n", encoding="utf-8")
    (stdlib / "encodings" / "__init__.py").write_text(
        "synthetic encoding\n",
        encoding="utf-8",
    )
    (stdlib / "locale.py").write_text(
        "def normalize(value):\n    return value\n",
        encoding="utf-8",
    )
    (stdlib / "shutil.py").write_text(
        "class _TerminalSize:\n"
        "    columns = 80\n"
        "def get_terminal_size(fallback=(80, 24)):\n"
        "    return _TerminalSize()\n",
        encoding="utf-8",
    )
    runtime_native.parent.mkdir(parents=True, exist_ok=True)
    runtime_native.write_bytes(b"synthetic native extension\n")
    if not spec.windows:
        (stdlib / "native_alias.so").symlink_to(runtime_native)

    venv_python.parent.mkdir(parents=True, exist_ok=True)
    if spec.windows:
        venv_python.write_bytes(runtime_python.read_bytes())
        venv_python.chmod(0o755)
    else:
        venv_python.symlink_to(runtime_python)
    site_packages.mkdir(parents=True)
    (site_packages / "empty-installed-package").mkdir()
    (site_packages / "fixture_dependency.py").write_text(
        "VALUE = 'sealed'\n",
        encoding="utf-8",
    )
    (venv / "pyvenv.cfg").write_text(
        "home = /build-machine/python\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    if not spec.windows:
        (site_packages / "native_alias.so").symlink_to(runtime_native)

    application = base / "application-source"
    (application / "ecosystem/defaultspack/defaultspack").mkdir(parents=True)
    (application / "core_runtime/host_broker").mkdir(parents=True)
    (application / "empty-application-package").mkdir()
    (application / "app.py").write_text(
        "import json, os\n"
        "def prepare_for_sealed_dispatch(scope):\n"
        "    if scope.app_root_for(__file__).name != 'app':\n"
        "        raise RuntimeError('invalid sealed app root')\n"
        "    if scope.packvm_bundle_binding_for(__file__) is not None:\n"
        "        raise RuntimeError('unexpected PackVM bundle binding')\n"
        "def main(argv=None):\n"
        "    with open(os.environ['ROLE_MARKER'], 'a') as handle:\n"
        "        handle.write(json.dumps(['typed', list(argv or [])]) + '\\n')\n"
        "    return 7\n",
        encoding="utf-8",
    )
    (application / "ecosystem/defaultspack/defaultspack/desktop_app.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "def prepare_for_sealed_dispatch(scope):\n"
        "    app_root = scope.app_root_for(__file__)\n"
        "    pack_root = Path(__file__).resolve().parents[1]\n"
        "    if str(pack_root) not in sys.path:\n"
        "        sys.path.insert(0, str(pack_root))\n"
        "    if str(app_root) not in sys.path:\n"
        "        sys.path.insert(0, str(app_root))\n"
        "def main(argv=None):\n"
        "    with open(os.environ['ROLE_MARKER'], 'a') as handle:\n"
        "        handle.write(json.dumps(['defaultspack', list(argv or [])]) + '\\n')\n"
        "    return 8\n",
        encoding="utf-8",
    )
    (application / "core_runtime/host_broker/computer_host_helper.py").write_text(
        "import json, os, sys\n"
        "def main():\n"
        "    request = json.loads(sys.stdin.read())\n"
        "    with open(os.environ['ROLE_MARKER'], 'a') as handle:\n"
        "        handle.write(json.dumps(['host_helper', request]) + '\\n')\n"
        "    print(json.dumps({'ok': True}))\n"
        "    return 9\n",
        encoding="utf-8",
    )

    output = base / "snapshot-not-python-runtime"
    BUILDER.assemble_environment(
        output,
        runtime,
        venv,
        target,
        release_digest="a" * 64,
        application_source=application,
    )
    return runtime, venv, output


def _fixture_sys_path(output: Path, *, include_missing_zip: bool = False) -> list[str]:
    """Return the exact Unix import roots emitted by isolated CPython."""
    entries = [
        output / "runtime/lib/python3.13",
        output / "runtime/lib/python3.13/lib-dynload",
        output / "venv/lib/python3.13/site-packages",
    ]
    if include_missing_zip:
        entries.insert(0, output / "runtime/lib/python313.zip")
    return [str(path) for path in entries]


def _install_runtime_overlay(output: Path) -> tuple[str, str]:
    """Install the sole Host overlay after the sealed base fixture is verified."""
    manifest_path = output / BUILDER.MANIFEST_FILENAME
    sealed = json.loads(manifest_path.read_text(encoding="utf-8"))
    outer_digest = "f" * 64
    document = {
        "schema": "io.tobkiri.runtime-resource-manifest.v1",
        "overlay": {
            "schema": "io.tobkiri.sealed-runtime-overlay.v1",
            "outer_manifest_sha256": outer_digest,
            "sealed_manifest_sha256": BUILDER._sha256_file(manifest_path),
        },
        "entries": [
            {
                "path": entry["path"][len("app/") :],
                "size": entry["size"],
                "sha256": entry["sha256"],
            }
            for entry in sealed["files"]
            if entry["path"].startswith("app/")
        ],
    }
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    app = output / "app"
    if os.name != "nt":
        app.chmod(0o755)
    overlay = app / "runtime-resource-manifest.v1.json"
    overlay.write_bytes(payload)
    if os.name != "nt":
        overlay.chmod(0o444)
        app.chmod(0o555)
    return hashlib.sha256(payload).hexdigest(), outer_digest


def _runtime_overlay_arguments(output: Path) -> list[str]:
    overlay_digest, outer_digest = _install_runtime_overlay(output)
    return [
        "--runtime-overlay-sha256",
        overlay_digest,
        "--outer-runtime-manifest-sha256",
        outer_digest,
        "--application-bundle-root",
        "",
        "--packvm-provisioning-sha256",
        "",
        "--packvm-helper-manifest-sha256",
        "",
        "--packvm-helper-team-id",
        "",
    ]


def _packvm_bundle_fixture(root: Path) -> tuple[Path, dict[str, str]]:
    """Create a signed-app-mode macOS resource domain for v3 binding tests."""

    bundle = root / "Tobkiri Launcher.app"
    resources = bundle / "Contents" / "Resources"
    application = resources / "app"
    application.mkdir(parents=True)
    files = {
        resources / "packvm-vz-provisioning.v1.json": b'{"kind":"provisioning"}\n',
        resources / "packvm-vz-helper.manifest.v1.json": b'{"kind":"helper"}\n',
        application / "runtime-resource-manifest.v1.json": b'{"kind":"runtime"}\n',
    }
    for path, payload in files.items():
        path.write_bytes(payload)
        path.chmod(0o644)
    for directory in (application, resources, resources.parent, bundle):
        directory.chmod(0o755)
    return bundle, {
        "provisioning": hashlib.sha256(
            files[resources / "packvm-vz-provisioning.v1.json"]
        ).hexdigest(),
        "helper": hashlib.sha256(
            files[resources / "packvm-vz-helper.manifest.v1.json"]
        ).hexdigest(),
        "outer": hashlib.sha256(
            files[application / "runtime-resource-manifest.v1.json"]
        ).hexdigest(),
    }


def _writable_staged_fixture(base: Path, target: str) -> Path:
    """Copy one valid sealed fixture into a writable publish stage."""
    sealed = _fixture_sources(base / "fixture", target)[2]
    staged = base / "staged-python-runtime"
    BUILDER._copy_tree(sealed, staged, BUILDER.target_spec(target))
    return staged


def test_staged_environment_is_verified_before_final_seal(tmp_path: Path) -> None:
    """Placement leaves a writable tree until destination verification finishes."""
    target = "x86_64-unknown-linux-gnu"
    staged = _writable_staged_fixture(tmp_path, target)
    output = tmp_path / "published-python-runtime"

    BUILDER._publish_staged_environment(staged, output, target)

    assert not staged.exists()
    assert output.stat().st_mode & 0o222
    assert BUILDER.validate_environment(
        output,
        target,
        run_native_smoke=False,
        require_sealed=False,
    )

    BUILDER._freeze_tree(output, BUILDER.target_spec(target))
    assert not output.stat().st_mode & 0o222
    BUILDER.validate_environment(output, target, run_native_smoke=False)


def test_staged_environment_supports_exdev_before_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EXDEV fallback copies writable bytes, then publishes atomically."""
    target = "x86_64-unknown-linux-gnu"
    staged = _writable_staged_fixture(tmp_path, target)
    output = tmp_path / "published-python-runtime"
    original_replace = os.replace
    calls = 0

    def replace_with_exdev(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EXDEV, "simulated cross-device rename")
        original_replace(source, destination)

    monkeypatch.setattr(BUILDER.os, "replace", replace_with_exdev)
    BUILDER._publish_staged_environment(staged, output, target)

    assert calls == 2
    assert staged.is_dir()
    assert staged.stat().st_mode & 0o222
    assert output.stat().st_mode & 0o222
    BUILDER.validate_environment(
        output,
        target,
        run_native_smoke=False,
        require_sealed=False,
    )


@pytest.mark.parametrize("destination_kind", ("directory", "symlink"))
def test_staged_environment_rejects_preexisting_destination(
    tmp_path: Path,
    destination_kind: str,
) -> None:
    """A preexisting directory or symlink is never replaced by the stage."""
    target = "x86_64-unknown-linux-gnu"
    staged = _writable_staged_fixture(tmp_path, target)
    output = tmp_path / "published-python-runtime"
    if destination_kind == "directory":
        output.mkdir()
        (output / "sentinel").write_text("untouched\n", encoding="utf-8")
    else:
        external = tmp_path / "external"
        external.mkdir()
        (external / "sentinel").write_text("untouched\n", encoding="utf-8")
        output.symlink_to(external, target_is_directory=True)

    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="publish destination already exists",
    ):
        BUILDER._publish_staged_environment(staged, output, target)

    assert staged.is_dir()
    if destination_kind == "directory":
        assert (output / "sentinel").read_text(encoding="utf-8") == "untouched\n"
    else:
        assert output.is_symlink()
        assert (output / "sentinel").read_text(encoding="utf-8") == "untouched\n"


def test_staged_environment_failure_cleans_published_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed destination verification removes only the newly published tree."""
    target = "x86_64-unknown-linux-gnu"
    staged = _writable_staged_fixture(tmp_path, target)
    output = tmp_path / "published-python-runtime"

    def reject_destination(root: Path, *_args: object, **_kwargs: object) -> str:
        if Path(root) == output:
            raise BUILDER.SealedEnvironmentError("simulated destination failure")
        raise AssertionError(f"unexpected validation path: {root}")

    monkeypatch.setattr(BUILDER, "validate_environment", reject_destination)
    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="simulated destination failure",
    ):
        BUILDER._publish_staged_environment(staged, output, target)

    assert not output.exists()
    assert not output.is_symlink()


def test_rootless_packaging_binding_and_identity_safe_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Formal packaging binds and removes only a complete private sealed tree."""
    target = "x86_64-pc-windows-msvc"
    _runtime, _venv, output = _fixture_sources(tmp_path, target)
    digest = BUILDER.validate_environment(output, target, run_native_smoke=False)
    monkeypatch.setenv("RUNNER_TEMP", os.fspath(tmp_path))
    binding = tmp_path / "packaging.env"
    source_snapshot = tmp_path / "source-snapshot"
    source_snapshot.mkdir(mode=0o500)
    BUILDER._write_packaging_binding(
        binding,
        output,
        digest,
        BUILDER.target_spec(target),
        source_snapshot,
        "b" * 40,
        "c" * 64,
        "d" * 64,
    )
    payload = binding.read_text(encoding="utf-8")
    assert f"TOBKIRI_PACKAGING_PYTHON_SNAPSHOT={output}\n" in payload
    assert f"TOBKIRI_PACKAGING_PYTHON_INVENTORY_SHA256={digest}\n" in payload
    os.environ[BUILDER.MANIFEST_SHA_ENV] = digest
    try:
        assert (
            BUILDER.main(["--target", target, "--output-root", os.fspath(output), "--cleanup"]) == 0
        )
    finally:
        os.environ.pop(BUILDER.MANIFEST_SHA_ENV, None)
    assert not output.exists()


def test_rootless_cleanup_rejects_name_swap_and_tamper(tmp_path: Path) -> None:
    """A linked or changed candidate is never adopted as cleanup authority."""
    target = "x86_64-pc-windows-msvc"
    _runtime, _venv, output = _fixture_sources(tmp_path, target)
    digest = BUILDER.validate_environment(output, target, run_native_smoke=False)
    replacement = tmp_path / "replacement"
    output.chmod(0o700)
    output.rename(replacement)
    output.symlink_to(replacement, target_is_directory=True)
    os.environ[BUILDER.MANIFEST_SHA_ENV] = digest
    try:
        assert (
            BUILDER.main(["--target", target, "--output-root", os.fspath(output), "--cleanup"]) == 1
        )
    finally:
        os.environ.pop(BUILDER.MANIFEST_SHA_ENV, None)
    assert output.is_symlink() and replacement.is_dir()


def test_explicit_uv_authority_is_private_absolute_and_digest_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private external uv stage is accepted without mutating the checkout."""
    target = "aarch64-apple-darwin"
    root = tmp_path / "uv-stage"
    bundled = root / "bundled"
    bundled.mkdir(parents=True, mode=0o700)
    uv = bundled / "uv"
    uv.write_bytes(b"pinned private uv")
    uv.chmod(0o555)
    monkeypatch.setitem(
        BUILDER.UV_BINARY_SHA256_BY_TARGET,
        target,
        hashlib.sha256(uv.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(BUILDER, "_uv_version", lambda *_args: None)
    assert BUILDER._validate_pinned_uv_executable(ROOT, uv, BUILDER.target_spec(target)) == uv
    bundled.chmod(0o755)
    with pytest.raises(BUILDER.SealedEnvironmentError, match="not private"):
        BUILDER._validate_pinned_uv_executable(ROOT, uv, BUILDER.target_spec(target))


def test_macos_python_archive_authority_is_exact_and_offline_after_download(
    tmp_path: Path,
) -> None:
    """Each mac target names one reviewed PBS revision/digest and safe payload."""
    assert BUILDER.PYTHON_BUILD_REVISION == "20260510"
    assert set(BUILDER.PYTHON_ARCHIVE_SHA256_BY_TARGET) == {
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
    }
    for target, digest in BUILDER.PYTHON_ARCHIVE_SHA256_BY_TARGET.items():
        assert len(digest) == 64
        url = BUILDER._python_archive_url(BUILDER.target_spec(target))
        assert f"cpython-3.13.13%2B20260510-{target}" in url
        assert url.endswith("-install_only_stripped.tar.gz")
    source = BUILDER_PATH.read_text(encoding="utf-8")
    assert '"python",\n                "install"' not in source

    archive = tmp_path / "python.tar.gz"
    payload = tmp_path / "python"
    payload.mkdir()
    executable = payload / "bin/python3"
    executable.parent.mkdir()
    executable.write_bytes(b"python")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname="python")
    extracted = BUILDER._extract_pinned_python_archive(archive, tmp_path / "output")
    assert (extracted / "bin/python3").read_bytes() == b"python"


def test_formal_packaging_lock_selection_is_target_bound() -> None:
    """ARM uses its reviewed wheel lock while Intel publication fails closed."""
    arm = BUILDER.target_spec("aarch64-apple-darwin")
    assert BUILDER.packaging_requirements_relative(arm) == Path(
        "tobkiri_runtime/requirements-packaging-aarch64-apple-darwin.txt"
    )

    intel = BUILDER.target_spec("x86_64-apple-darwin")
    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="cryptography 50.0.0 has no CPython 3.13 macOS x86_64 wheel",
    ):
        BUILDER.packaging_requirements_relative(intel)

    linux = BUILDER.target_spec("x86_64-unknown-linux-gnu")
    assert BUILDER.packaging_requirements_relative(linux) == BUILDER.DEFAULT_REQUIREMENTS_RELATIVE


def test_formal_builder_rejects_external_requirements_path(tmp_path: Path) -> None:
    """Formal builds cannot be redirected to a caller-selected lock file."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    external_requirements = tmp_path / "requirements.txt"
    external_requirements.write_text("# external\n", encoding="utf-8")

    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="formal build does not accept an external requirements path",
    ):
        BUILDER.build_environment(
            repo_root,
            "x86_64-unknown-linux-gnu",
            requirements_path=external_requirements,
        )


def test_pinned_python_archive_download_keeps_sha256_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pinned archive download still verifies its exact SHA-256 digest."""
    spec = BUILDER.target_spec("x86_64-apple-darwin")
    payload = b"pinned archive bytes\n"

    class Response:
        def __init__(self) -> None:
            self._read = False

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def read(self, _size: int) -> bytes:
            if self._read:
                return b""
            self._read = True
            return payload

    monkeypatch.setattr(
        BUILDER.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    monkeypatch.setitem(
        BUILDER.PYTHON_ARCHIVE_SHA256_BY_TARGET,
        spec.triple,
        hashlib.sha256(payload).hexdigest(),
    )
    verified = tmp_path / "verified.tar.gz"
    BUILDER._download_pinned_python_archive(spec, verified)
    assert verified.read_bytes() == payload

    monkeypatch.setitem(BUILDER.PYTHON_ARCHIVE_SHA256_BY_TARGET, spec.triple, "0" * 64)
    with pytest.raises(BUILDER.SealedEnvironmentError, match="SHA-256 mismatch"):
        BUILDER._download_pinned_python_archive(spec, tmp_path / "mismatch.tar.gz")


def test_pinned_python_archive_materializes_idle3_like_internal_links(
    tmp_path: Path,
) -> None:
    """Internal PBS aliases are copied from validated regular archive targets."""
    archive = tmp_path / "internal-links.tar.gz"
    root, root_payload = _archive_member("python", member_type=tarfile.DIRTYPE)
    bin_dir, bin_payload = _archive_member(
        "python/bin",
        member_type=tarfile.DIRTYPE,
    )
    lib_dir, lib_payload = _archive_member(
        "python/lib",
        member_type=tarfile.DIRTYPE,
    )
    idle_target, idle_payload = _archive_member(
        "python/bin/idle3.13",
        payload=b"#!/usr/bin/env python3\nprint('idle')\n",
    )
    idle_target.mode = 0o775
    idle_link, idle_link_payload = _archive_member(
        "python/bin/idle3",
        member_type=tarfile.SYMTYPE,
        linkname="idle3.13",
    )
    hard_link, hard_link_payload = _archive_member(
        "python/bin/idle3-hardlink",
        member_type=tarfile.LNKTYPE,
        linkname="python/bin/idle3.13",
    )
    lib_link, lib_link_payload = _archive_member(
        "python/lib-alias",
        member_type=tarfile.SYMTYPE,
        linkname="lib",
    )
    _write_archive(
        archive,
        (
            (root, root_payload),
            (bin_dir, bin_payload),
            (lib_dir, lib_payload),
            (idle_target, idle_payload),
            (idle_link, idle_link_payload),
            (hard_link, hard_link_payload),
            (lib_link, lib_link_payload),
        ),
    )

    extracted = BUILDER._extract_pinned_python_archive(archive, tmp_path / "output")
    expected = (extracted / "bin/idle3.13").read_bytes()
    for alias in ("idle3", "idle3-hardlink"):
        path = extracted / "bin" / alias
        assert path.read_bytes() == expected
        assert not path.is_symlink()
        assert path.stat().st_mode & 0o777 == 0o755
    directory_alias = extracted / "lib-alias"
    assert directory_alias.is_symlink()
    assert directory_alias.resolve() == extracted / "lib"


def test_pinned_python_archive_creates_implicit_parent_directories(
    tmp_path: Path,
) -> None:
    """PBS archives may omit directory entries for the python tree."""
    archive = tmp_path / "implicit-parents.tar.gz"
    target, target_payload = _archive_member(
        "python/bin/idle3.13",
        payload=b"idle\n",
    )
    link, link_payload = _archive_member(
        "python/bin/idle3",
        member_type=tarfile.SYMTYPE,
        linkname="idle3.13",
    )
    _write_archive(archive, ((target, target_payload), (link, link_payload)))

    extracted = BUILDER._extract_pinned_python_archive(archive, tmp_path / "output")
    assert (extracted / "bin").is_dir()
    assert (extracted / "bin/idle3.13").read_bytes() == b"idle\n"
    assert (extracted / "bin/idle3").read_bytes() == b"idle\n"
    assert not (extracted / "bin/idle3").is_symlink()


def test_pinned_python_archive_omits_safe_archive_bytecode(
    tmp_path: Path,
) -> None:
    """Safe pinned bytecode is validated but never copied into the runtime."""
    archive = tmp_path / "archive-bytecode.tar.gz"
    root, root_payload = _archive_member("python", member_type=tarfile.DIRTYPE)
    executable, executable_payload = _archive_member(
        "python/bin/python3",
        payload=b"exact executable bytes\n",
    )
    module, module_payload = _archive_member(
        "python/lib/python3.13/encodings.py",
        payload=b"exact module bytes\n",
    )
    pyc, pyc_payload = _archive_member(
        "python/lib/python3.13/encodings.pyc",
        payload=b"archive bytecode\n",
    )
    cached_pyc, cached_pyc_payload = _archive_member(
        "python/lib/python3.13/__pycache__/encodings.cpython-313.pyc",
        payload=b"cached archive bytecode\n",
    )
    _write_archive(
        archive,
        (
            (root, root_payload),
            (executable, executable_payload),
            (module, module_payload),
            (pyc, pyc_payload),
            (cached_pyc, cached_pyc_payload),
        ),
    )

    extracted = BUILDER._extract_pinned_python_archive(archive, tmp_path / "output")
    assert (extracted / "bin/python3").read_bytes() == b"exact executable bytes\n"
    assert (extracted / "lib/python3.13/encodings.py").read_bytes() == (b"exact module bytes\n")
    assert not (extracted / "lib/python3.13/encodings.pyc").exists()
    assert not (extracted / "lib/python3.13/__pycache__").exists()
    assert not any(
        path.suffix.lower() in {".pyc", ".pyo"}
        or "__pycache__" in path.relative_to(extracted).parts
        for path in extracted.rglob("*")
    )


def _archive_member(
    name: str,
    *,
    member_type: bytes = tarfile.REGTYPE,
    payload: bytes = b"payload\n",
    linkname: str = "",
) -> tuple[tarfile.TarInfo, bytes]:
    """Build one deterministic tar member for extraction safety cases."""
    member = tarfile.TarInfo(name)
    member.type = member_type
    member.mode = 0o755 if member_type == tarfile.DIRTYPE else 0o644
    member.linkname = linkname
    if member_type == tarfile.REGTYPE:
        member.size = len(payload)
    return member, payload


def _write_archive(
    path: Path,
    members: tuple[tuple[tarfile.TarInfo, bytes], ...],
) -> None:
    """Write synthetic members without filesystem traversal or link following."""
    with tarfile.open(path, "w:gz") as bundle:
        for member, payload in members:
            bundle.addfile(
                member,
                io.BytesIO(payload) if member.isreg() else None,
            )


@pytest.mark.parametrize(
    ("name", "member_type", "linkname"),
    (
        ("python/../outside", tarfile.REGTYPE, ""),
        ("/outside", tarfile.REGTYPE, ""),
        ("python/bin/link", tarfile.SYMTYPE, "/outside"),
        ("python/bin/hardlink", tarfile.LNKTYPE, "../../outside"),
        ("python/bin/device", tarfile.CHRTYPE, ""),
        ("python/bin/fifo", tarfile.FIFOTYPE, ""),
        ("python/bin/special", b"?", ""),
    ),
)
def test_pinned_python_archive_rejects_unsafe_member_types_and_paths(
    tmp_path: Path,
    name: str,
    member_type: bytes,
    linkname: str,
) -> None:
    """Archive extraction rejects traversal, links, devices, FIFOs, and specials."""
    archive = tmp_path / "unsafe.tar.gz"
    root, root_payload = _archive_member("python", member_type=tarfile.DIRTYPE)
    unsafe, unsafe_payload = _archive_member(
        name,
        member_type=member_type,
        linkname=linkname,
    )
    _write_archive(archive, ((root, root_payload), (unsafe, unsafe_payload)))

    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="(unsafe pinned|escapes)",
    ):
        BUILDER._extract_pinned_python_archive(archive, tmp_path / "output")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize(
    "members",
    (
        (
            _archive_member("python", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin", member_type=tarfile.DIRTYPE),
            _archive_member(
                "python/bin/link",
                member_type=tarfile.SYMTYPE,
                linkname="../../outside",
            ),
        ),
        (
            _archive_member("python", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin", member_type=tarfile.DIRTYPE),
            _archive_member(
                "python/bin/link",
                member_type=tarfile.SYMTYPE,
                linkname="missing",
            ),
        ),
        (
            _archive_member("python", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin", member_type=tarfile.DIRTYPE),
            _archive_member(
                "python/bin/first",
                member_type=tarfile.SYMTYPE,
                linkname="second",
            ),
            _archive_member(
                "python/bin/second",
                member_type=tarfile.SYMTYPE,
                linkname="first",
            ),
        ),
        (
            _archive_member("python", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin/special", member_type=tarfile.CHRTYPE),
            _archive_member(
                "python/bin/link",
                member_type=tarfile.SYMTYPE,
                linkname="special",
            ),
        ),
        (
            _archive_member("python", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin", member_type=tarfile.DIRTYPE),
            _archive_member(
                "python/bin/first",
                member_type=tarfile.LNKTYPE,
                linkname="python/bin/second",
            ),
            _archive_member(
                "python/bin/second",
                member_type=tarfile.LNKTYPE,
                linkname="python/bin/first",
            ),
        ),
        (
            _archive_member("python", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin/hidden.pyc"),
            _archive_member(
                "python/bin/symlink-to-bytecode",
                member_type=tarfile.SYMTYPE,
                linkname="hidden.pyc",
            ),
        ),
        (
            _archive_member("python", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin/hidden.pyo"),
            _archive_member(
                "python/bin/hardlink-to-bytecode",
                member_type=tarfile.LNKTYPE,
                linkname="python/bin/hidden.pyo",
            ),
        ),
    ),
)
def test_pinned_python_archive_rejects_link_graph_attacks(
    tmp_path: Path,
    members: tuple[tuple[tarfile.TarInfo, bytes], ...],
) -> None:
    """Link targets must be internal, present, non-special, and acyclic."""
    archive = tmp_path / "link-attack.tar.gz"
    _write_archive(archive, members)

    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="(unsafe pinned|escapes|missing|cycle|excluded)",
    ):
        BUILDER._extract_pinned_python_archive(archive, tmp_path / "output")


@pytest.mark.parametrize(
    "members",
    (
        (_archive_member("python2/bin/python3"),),
        (_archive_member("other/bin/python3"),),
        (
            _archive_member(
                "python",
                member_type=tarfile.SYMTYPE,
                linkname="python2",
            ),
        ),
        (_archive_member("python", payload=b"not-a-directory\n"),),
    ),
)
def test_pinned_python_archive_rejects_missing_or_invalid_root(
    tmp_path: Path,
    members: tuple[tuple[tarfile.TarInfo, bytes], ...],
) -> None:
    """Only python-rooted entries or a real python directory are accepted."""
    archive = tmp_path / "invalid-root.tar.gz"
    _write_archive(archive, members)

    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="(unsafe pinned|python directory)",
    ):
        BUILDER._extract_pinned_python_archive(archive, tmp_path / "output")


@pytest.mark.parametrize(
    "members",
    (
        (
            _archive_member("python", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin/python3"),
            _archive_member("python/bin/python3", payload=b"different\n"),
        ),
        (
            _archive_member("python", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin", payload=b"file\n"),
            _archive_member("python/bin/child", payload=b"child\n"),
        ),
        (
            _archive_member("python", member_type=tarfile.DIRTYPE),
            _archive_member("python/bin", payload=b"file\n"),
            _archive_member("python/BIN/child", payload=b"case\n"),
        ),
    ),
)
def test_pinned_python_archive_rejects_duplicate_and_prefix_collisions(
    tmp_path: Path,
    members: tuple[tuple[tarfile.TarInfo, bytes], ...],
) -> None:
    """No duplicate or file/prefix collision may reach the extraction phase."""
    archive = tmp_path / "collision.tar.gz"
    _write_archive(archive, members)

    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="(duplicate|file/prefix collision)",
    ):
        BUILDER._extract_pinned_python_archive(archive, tmp_path / "output")


def test_pinned_python_archive_rejects_destination_symlink(tmp_path: Path) -> None:
    """A pre-existing destination symlink is never followed by extraction."""
    archive = tmp_path / "valid.tar.gz"
    executable, executable_payload = _archive_member("python/bin/python3")
    _write_archive(
        archive,
        ((executable, executable_payload),),
    )
    real_destination = tmp_path / "real-output"
    real_destination.mkdir()
    destination = tmp_path / "output"
    destination.symlink_to(real_destination, target_is_directory=True)

    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER._extract_pinned_python_archive(archive, destination)
    assert not (real_destination / "python").exists()


def test_committed_source_inventory_copies_exact_bytes_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir(mode=0o500)
    payloads = {
        "tobkiri_runtime/docs/managed-sandbox-runtime-implementation-plan.md": (
            b"implementation plan\n"
        ),
        "tobkiri_runtime/docs/managed-sandbox-runtime/01-overview.md": (b"overview\n"),
        "tobkiri_runtime/module.py": b"VALUE = 1\n",
        "tobkiri_runtime/packaged_defaultspack_source_manifest.v1.json": b"{}\n",
    }
    entries = []
    source.chmod(0o700)
    for relative, payload in payloads.items():
        path = source / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o400)
        entries.append(
            {
                "path": relative,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "executable": False,
            }
        )
    manifest_digest = next(
        entry["sha256"]
        for entry in entries
        if entry["path"] == "tobkiri_runtime/packaged_defaultspack_source_manifest.v1.json"
    )
    document = {
        "schema": BUILDER.SOURCE_SNAPSHOT_SCHEMA,
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "source_manifest_sha256": manifest_digest,
        "files": entries,
    }
    encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    inventory_digest = hashlib.sha256(encoded).hexdigest()
    (source / BUILDER.SOURCE_SNAPSHOT_MANIFEST).write_bytes(encoded)
    (source / BUILDER.SOURCE_SNAPSHOT_MANIFEST).chmod(0o400)
    for directory in sorted((path for path in source.rglob("*") if path.is_dir()), reverse=True):
        directory.chmod(0o500)
    source.chmod(0o500)
    release_frame = {
        "schema": BUILDER.SOURCE_SNAPSHOT_SCHEMA,
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "source_manifest_sha256": manifest_digest,
        "source_inventory_sha256": inventory_digest,
    }
    release_digest = hashlib.sha256(
        (json.dumps(release_frame, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    copied = BUILDER._copy_verified_source_snapshot(
        source, tmp_path / "copied", inventory_digest, release_digest
    )
    assert (copied / "tobkiri_runtime/module.py").read_bytes() == b"VALUE = 1\n"
    source.chmod(0o700)
    (source / "tobkiri_runtime").chmod(0o700)
    extra = source / "tobkiri_runtime" / "unexpected.py"
    extra.write_bytes(b"EXTRA = True\n")
    extra.chmod(0o400)
    (source / "tobkiri_runtime").chmod(0o500)
    source.chmod(0o500)
    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="missing or extra files",
    ):
        BUILDER._copy_verified_source_snapshot(
            source, tmp_path / "rejected-extra", inventory_digest, release_digest
        )
    source.chmod(0o700)
    (source / "tobkiri_runtime").chmod(0o700)
    extra.unlink()
    (source / "tobkiri_runtime").chmod(0o500)
    module = source / "tobkiri_runtime/module.py"
    module.chmod(0o600)
    module.write_bytes(b"ATTACKER = True\n")
    module.chmod(0o400)
    source.chmod(0o500)
    with pytest.raises(BUILDER.SealedEnvironmentError, match="bytes changed"):
        BUILDER._copy_verified_source_snapshot(
            source, tmp_path / "rejected", inventory_digest, release_digest
        )


@pytest.mark.parametrize(
    "target",
    ("x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"),
)
def test_manifest_is_strict_complete_and_reproducible(tmp_path: Path, target: str) -> None:
    """Both fixed platform layouts produce byte-identical sealed manifests."""
    first = _fixture_sources(tmp_path / "first", target)[2]
    second = _fixture_sources(tmp_path / "second", target)[2]
    first_manifest = first / BUILDER.MANIFEST_FILENAME
    second_manifest = second / BUILDER.MANIFEST_FILENAME

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    first_document = json.loads(first_manifest.read_text(encoding="utf-8"))
    assert tuple(first_document) == BUILDER.MANIFEST_KEYS
    assert tuple(first_document["package_provenance"]) == (
        "kind",
        "package_id",
        "release_digest",
    )
    assert first_document["package_provenance"]["package_id"] == "dev.tobkiri.launcher"
    assert tuple(first_document["sentinels"]) == BUILDER.SENTINEL_KEYS
    records = first_document["files"]
    assert records == sorted(records, key=lambda entry: entry["path"])
    assert all(entry.keys() == set(BUILDER.FILE_KEYS) for entry in records)
    assert BUILDER.MANIFEST_FILENAME not in {entry["path"] for entry in records}
    assert "lease.v1" in {entry["path"] for entry in records}
    assert first_document["environment_digest"] == BUILDER._files_digest(records)
    assert BUILDER.validate_environment(first, target, run_native_smoke=False)


def test_macos_builder_bootstrap_and_schema_share_exact_package_provenance(
    tmp_path: Path,
) -> None:
    """The generated macOS identity is accepted unchanged by every consumer."""
    output = _fixture_sources(tmp_path, "x86_64-unknown-linux-gnu")[2]
    document = json.loads((output / BUILDER.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    document["platform"] = "macos"
    document["architecture"] = "x86_64"
    document["package_provenance"]["kind"] = "pinned-python-build-standalone-v1"

    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    old_path = sys.path[:]
    old_package = sys.modules.pop("tobkiri_sealed", None)
    old_bootstrap = sys.modules.pop("tobkiri_sealed.bootstrap", None)
    try:
        sys.path.insert(0, str(source_root))
        import tobkiri_sealed.bootstrap as bootstrap

        assert BUILDER._validate_manifest_shape(document) == document
        assert bootstrap._validate_manifest_shape(document) == document

        for field, invalid in (
            ("kind", "apple-code-signature-v1"),
            ("package_id", "dev.tobkiri.other"),
            ("release_digest", "sha256:" + "a" * 64),
        ):
            tampered = json.loads(json.dumps(document))
            tampered["package_provenance"][field] = invalid
            with pytest.raises(BUILDER.SealedEnvironmentError):
                BUILDER._validate_manifest_shape(tampered)
            with pytest.raises(bootstrap.SealedBootstrapError):
                bootstrap._validate_manifest_shape(tampered)

        extra_field = json.loads(json.dumps(document))
        extra_field["package_provenance"]["source_path"] = "/private/source"
        with pytest.raises(BUILDER.SealedEnvironmentError, match="shape"):
            BUILDER._validate_manifest_shape(extra_field)
        with pytest.raises(bootstrap.SealedBootstrapError, match="shape"):
            bootstrap._validate_manifest_shape(extra_field)

        environment_schema = json.loads(
            (ROOT / ".github" / "schemas" / "sealed-python-environment.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        assert environment_schema["properties"]["package_provenance"]["properties"]["kind"][
            "enum"
        ] == [
            "pinned-python-build-standalone-v1",
            "windows-authenticode-v1",
            "linux-immutable-package-v1",
        ]
    finally:
        sys.path = old_path
        sys.modules.pop("tobkiri_sealed.bootstrap", None)
        sys.modules.pop("tobkiri_sealed", None)
        if old_package is not None:
            sys.modules["tobkiri_sealed"] = old_package
        if old_bootstrap is not None:
            sys.modules["tobkiri_sealed.bootstrap"] = old_bootstrap


def test_manifest_contains_fixed_entrypoints_and_bootstrap_paths(tmp_path: Path) -> None:
    """The Unix layout inventories every fixed role and installed bootstrap."""
    output = _fixture_sources(tmp_path, "x86_64-unknown-linux-gnu")[2]
    paths = {
        entry["path"]
        for entry in json.loads((output / BUILDER.MANIFEST_FILENAME).read_text(encoding="utf-8"))[
            "files"
        ]
    }
    assert {
        "sealed-directory-modes.v1.json",
        "lease.v1",
        "venv/bin/python3",
        "venv/lib/python3.13/site-packages/tobkiri_sealed/bootstrap.py",
        "app/kernel_entry.py",
        "app/defaultspack_entry.py",
        "app/host_helper_entry.py",
        "sentinels/stdlib.sha256",
        "sentinels/site-packages.sha256",
        "sentinels/native.sha256",
    } <= paths
    assert {
        "app/app.py",
        "app/ecosystem/defaultspack/defaultspack/desktop_app.py",
        "app/core_runtime/host_broker/computer_host_helper.py",
    } <= paths


def test_directory_inventory_is_exact_file_parent_closure(tmp_path: Path) -> None:
    """Producer prunes empty inputs and both verifiers reject every extra dir."""
    output = _fixture_sources(tmp_path / "canonical", "x86_64-unknown-linux-gnu")[2]
    document = json.loads((output / BUILDER.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert not (output / "app/empty-application-package").exists()
    assert not (output / "venv/lib/python3.13/site-packages/empty-installed-package").exists()
    expected = BUILDER._expected_directories(document["files"])
    assert BUILDER._actual_directories(output) == expected

    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    old_path = sys.path[:]
    old_package = sys.modules.pop("tobkiri_sealed", None)
    old_bootstrap = sys.modules.pop("tobkiri_sealed.bootstrap", None)
    try:
        sys.path.insert(0, str(source_root))
        import tobkiri_sealed.bootstrap as bootstrap

        assert bootstrap._expected_directories(document["files"]) == expected
        for relative, builder_error in (
            ("empty-extra", "directory inventory"),
            ("__pycache__", "generated Python bytecode"),
            (
                "venv/lib/python3.13/site-packages-lookalike",
                "directory inventory",
            ),
        ):
            rejected = tmp_path / ("rejected-" + relative.replace("/", "-"))
            shutil.copytree(output, rejected)
            _make_test_mutable(rejected)
            extra = rejected / relative
            _make_test_mutable(extra.parent)
            extra.mkdir(parents=True)
            extra.chmod(0o555)
            extra.parent.chmod(0o555)
            rejected.chmod(0o555)
            with pytest.raises(
                BUILDER.SealedEnvironmentError,
                match=builder_error,
            ):
                BUILDER.validate_environment(
                    rejected,
                    "x86_64-unknown-linux-gnu",
                    run_native_smoke=False,
                )
            with pytest.raises(bootstrap.SealedBootstrapError):
                bootstrap._verify_tree(rejected, document)

        linked = tmp_path / "rejected-linked-directory"
        shutil.copytree(output, linked)
        _make_test_mutable(linked)
        (linked / "linked-extra").symlink_to(linked / "app", target_is_directory=True)
        linked.chmod(0o555)
        with pytest.raises(BUILDER.SealedEnvironmentError, match="link"):
            BUILDER.validate_environment(
                linked,
                "x86_64-unknown-linux-gnu",
                run_native_smoke=False,
            )
        with pytest.raises(bootstrap.SealedBootstrapError, match="link"):
            bootstrap._verify_tree(linked, document)

        omitted = json.loads(json.dumps(document))
        omitted["files"] = [
            entry for entry in omitted["files"] if entry["path"] != "app/kernel_entry.py"
        ]
        with pytest.raises(bootstrap.SealedBootstrapError, match="missing or extra files"):
            bootstrap._verify_tree(output, omitted)
    finally:
        sys.path = old_path
        sys.modules.pop("tobkiri_sealed.bootstrap", None)
        sys.modules.pop("tobkiri_sealed", None)
        if old_package is not None:
            sys.modules["tobkiri_sealed"] = old_package
        if old_bootstrap is not None:
            sys.modules["tobkiri_sealed.bootstrap"] = old_bootstrap


def test_assembly_materializes_links_and_freezes_the_complete_snapshot(
    tmp_path: Path,
) -> None:
    """The final resource has no links, bytecode, or write bits."""
    output = _fixture_sources(tmp_path, "x86_64-unknown-linux-gnu")[2]
    alias = output / "venv/lib/python3.13/site-packages/native_alias.so"
    assert alias.is_file()
    assert not alias.is_symlink()
    assert alias.read_bytes() == b"synthetic native extension\n"
    runtime_alias = output / "runtime/lib/python3.13/native_alias.so"
    assert runtime_alias.is_file()
    assert not runtime_alias.is_symlink()

    for path in (output, *output.rglob("*")):
        assert not path.is_symlink(), path
        expected_mode = 0o555 if path.is_dir() or path.stat().st_mode & 0o111 else 0o444
        assert stat.S_IMODE(path.stat().st_mode) == expected_mode, path
    assert all(
        not any(part == "__pycache__" for part in path.relative_to(output).parts)
        and path.suffix not in {".pyc", ".pyo"}
        for path in output.rglob("*")
    )

    evidence = json.loads((output / BUILDER.DIRECTORY_MODES_FILENAME).read_text(encoding="utf-8"))
    manifest = json.loads((output / BUILDER.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert evidence == BUILDER._directory_mode_document(manifest["files"])


@pytest.mark.parametrize("drift_mode", (0o755, 0o500))
def test_validator_rejects_exact_directory_mode_drift(
    tmp_path: Path,
    drift_mode: int,
) -> None:
    """Writable and merely non-writable-but-noncanonical directory modes fail."""
    output = _fixture_sources(tmp_path, "x86_64-unknown-linux-gnu")[2]
    directory = output / "runtime/lib"
    directory.chmod(drift_mode)
    with pytest.raises(BUILDER.SealedEnvironmentError, match="directory mode drift"):
        BUILDER.validate_environment(output, "x86_64-unknown-linux-gnu", run_native_smoke=False)


def test_final_venv_home_is_relative_to_the_sealed_launch_root(
    tmp_path: Path,
) -> None:
    """The copied PBS interpreter must resolve its home from the final root."""
    output = _fixture_sources(tmp_path, "x86_64-unknown-linux-gnu")[2]
    config = (output / "venv/pyvenv.cfg").read_text(encoding="utf-8")
    assert "home = runtime/bin\n" in config
    assert "home = ../runtime/bin\n" not in config


@pytest.mark.skipif(
    not os.environ.get("TOBKIRI_SEALED_NATIVE_SMOKE_PYTHON"),
    reason="set TOBKIRI_SEALED_NATIVE_SMOKE_PYTHON to run the standalone CPython relocation smoke",
)
def test_relocated_native_runtime_imports_encodings_and_installed_package(
    tmp_path: Path,
) -> None:
    """A moved regular executable finds PBS stdlib, native modules, and venv packages."""
    interpreter = Path(os.environ["TOBKIRI_SEALED_NATIVE_SMOKE_PYTHON"]).resolve(strict=True)
    source_lib = interpreter.parent.parent / "lib"
    stdlib_candidates = sorted(
        path for path in source_lib.iterdir() if path.is_dir() and path.name.startswith("python")
    )
    if len(stdlib_candidates) != 1:
        pytest.skip("standalone CPython stdlib layout is unavailable")
    stdlib = stdlib_candidates[0]
    minor = stdlib.name[len("python") :]
    root = tmp_path / "moved-runtime"
    runtime = root / "runtime"
    (runtime / "bin").mkdir(parents=True)
    shutil.copy2(interpreter, runtime / "bin/python3")
    shutil.copytree(source_lib, runtime / "lib", symlinks=False)
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    shutil.copy2(runtime / "bin/python3", venv / "bin/python3")
    (venv / "bin/python3").chmod(0o755)
    (venv / "pyvenv.cfg").write_text(
        "home = runtime/bin\ninclude-system-site-packages = false\nrelocatable = true\n",
        encoding="utf-8",
    )
    site_packages = venv / f"lib/python{minor}/site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / "installed_probe.py").write_text(
        "VALUE = 'installed-in-moved-venv'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            os.fspath(venv / "bin/python3"),
            "-I",
            "-B",
            "-c",
            (
                "import _ssl, encodings, json, sys, installed_probe; "
                "print(json.dumps({'executable': sys.executable, "
                "'prefix': sys.prefix, 'base_prefix': sys.base_prefix, "
                "'value': installed_probe.VALUE}, sort_keys=True))"
            ),
        ],
        cwd=root,
        env={**os.environ, BUILDER.PYTHON_BYTECODE_ENVIRONMENT: "1"},
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    for field, expected in (
        ("executable", venv / "bin/python3"),
        ("prefix", venv),
        ("base_prefix", runtime),
    ):
        value = Path(report[field])
        if not value.is_absolute():
            value = root / value
        assert value.resolve() == expected.resolve()
    assert report["value"] == "installed-in-moved-venv"


def test_validate_environment_rejects_later_child_python_bytecode(
    tmp_path: Path,
) -> None:
    """The final tree scanner rejects bytecode created after assembly."""
    output = _fixture_sources(tmp_path, "x86_64-unknown-linux-gnu")[2]
    runtime = output / "runtime/lib/python3.13"
    _make_test_mutable(runtime)
    encodings = runtime / "encodings"
    encodings.mkdir(exist_ok=True)
    _make_test_mutable(encodings)
    child_bytecode = encodings / "__pycache__/child.cpython-313.pyc"
    child_bytecode.parent.mkdir(parents=True)
    child_bytecode.write_bytes(b"child-created bytecode")

    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="generated Python bytecode",
    ):
        BUILDER.validate_environment(
            output,
            "x86_64-unknown-linux-gnu",
            run_native_smoke=False,
        )


@pytest.mark.parametrize("case", ("outside", "cycle"))
def test_venv_link_materializer_rejects_escape_and_cycle(
    tmp_path: Path,
    case: str,
) -> None:
    """Only links inside the assembly root may be materialized."""
    root = tmp_path / "assembly" / "venv"
    python_dir = root / "bin"
    python_dir.mkdir(parents=True)
    if case == "outside":
        target = tmp_path / "outside-python"
        target.write_bytes(b"outside\n")
        (python_dir / "python3").symlink_to(target)
    else:
        (python_dir / "python3").symlink_to(python_dir / "loop")
        (python_dir / "loop").symlink_to(python_dir / "python3")

    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER._materialize_venv_links(
            root,
            BUILDER.target_spec("x86_64-unknown-linux-gnu"),
        )


def test_runtime_link_materializer_rejects_outside_target(tmp_path: Path) -> None:
    """CPython runtime aliases cannot resolve outside their runtime root."""
    root = tmp_path / "runtime"
    (root / "lib").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside\n")
    (root / "lib" / "alias.so").symlink_to(outside)

    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER._materialize_runtime_links(
            root,
            BUILDER.target_spec("x86_64-unknown-linux-gnu"),
        )


def test_bootstrap_wire_dispatches_all_roles_and_publishes_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent wire reaches each canonical role with exact argv/stdin."""
    output = _fixture_sources(tmp_path / "sealed", "x86_64-unknown-linux-gnu")[2]
    overlay_arguments = _runtime_overlay_arguments(output)
    assert output.name != "python-runtime"
    marker = tmp_path / "roles.jsonl"

    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    old_prefix = sys.prefix
    old_base_prefix = sys.base_prefix
    old_executable = sys.executable
    old_path = sys.path[:]
    old_argv = sys.argv[:]
    old_dont_write_bytecode = sys.dont_write_bytecode
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    old_env = os.environ.copy()
    clean_env = _clean_sealed_test_environment()
    os.environ.clear()
    os.environ.update(clean_env)
    sys.path = _fixture_sys_path(output, include_missing_zip=True)
    sys.prefix = str(output / "venv")
    sys.base_prefix = str(output / "runtime")
    sys.executable = str(output / "venv/bin/python3")
    sys.dont_write_bytecode = True
    sys.modules.pop("tobkiri_sealed.bootstrap", None)
    sys.modules.pop("tobkiri_sealed", None)
    monkeypatch.setenv("ROLE_MARKER", str(marker))
    monkeypatch.setenv(
        "TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256",
        BUILDER._sha256_file(output / BUILDER.MANIFEST_FILENAME),
    )
    try:
        sys.path.insert(0, str(source_root))
        import tobkiri_sealed.bootstrap as bootstrap

        sys.path = _fixture_sys_path(output, include_missing_zip=True)

        for role, role_args, expected_return in (
            ("typed", ("--health",), 7),
            ("defaultspack", ("--port", "8766"), 8),
            ("host_helper", (), 9),
        ):
            attestation_dir = tmp_path / f"attestation-{role}"
            attestation_dir.mkdir()
            nonce = "a" * 64
            attestation = attestation_dir / f"startup-{nonce}.json"
            if role == "host_helper":
                sys.stdin = io.StringIO('{"function_id":"computer.observe"}')
                sys.stdout = io.StringIO()
            result = bootstrap.main(
                [
                    "--role",
                    role,
                    "--nonce",
                    nonce,
                    "--attestation",
                    str(attestation),
                    "--manifest",
                    str(output / BUILDER.MANIFEST_FILENAME),
                    "--environment-root",
                    str(output),
                    *overlay_arguments,
                    "--",
                    *role_args,
                ]
            )
            assert result == expected_return
            evidence = json.loads(attestation.read_text(encoding="utf-8"))
            assert list(evidence) == [
                "schema",
                "nonce",
                "role",
                "environment_digest",
                "executable",
                "prefix",
                "base_prefix",
                "sys_path",
                "stdlib_sha256",
                "site_packages_sha256",
                "native_sha256",
                "runtime_overlay_sha256",
                "outer_runtime_manifest_sha256",
                "lifetime_lease",
            ]
            assert evidence["role"] == role
            assert evidence["nonce"] == nonce
            assert evidence["lifetime_lease"] is True
            assert attestation.stat().st_mode & 0o777 == 0o600
            assert attestation.stat().st_nlink == 1
            if hasattr(os, "geteuid"):
                assert attestation.stat().st_uid == os.geteuid()
            assert all(Path(item).resolve().is_relative_to(output) for item in evidence["sys_path"])
            assert all(Path(item).resolve().is_relative_to(output) for item in sys.path)
            assert list(sys.path) == evidence["sys_path"]
            if role == "typed":
                with pytest.raises(bootstrap.SealedBootstrapError, match="already exists"):
                    bootstrap.main(
                        [
                            "--role",
                            role,
                            "--nonce",
                            nonce,
                            "--attestation",
                            str(attestation),
                            "--manifest",
                            str(output / BUILDER.MANIFEST_FILENAME),
                            "--environment-root",
                            str(output),
                            *overlay_arguments,
                            "--",
                            *role_args,
                        ]
                    )
            sys.path = _fixture_sys_path(output, include_missing_zip=True)
            sys.stdin = old_stdin
            sys.stdout = old_stdout
        records = [json.loads(line) for line in marker.read_text().splitlines()]
        assert records == [
            ["typed", ["--health"]],
            ["defaultspack", ["--port", "8766"]],
            ["host_helper", {"function_id": "computer.observe"}],
        ]
    finally:
        sys.prefix = old_prefix
        sys.base_prefix = old_base_prefix
        sys.executable = old_executable
        sys.path = old_path
        sys.argv = old_argv
        sys.dont_write_bytecode = old_dont_write_bytecode
        sys.stdin = old_stdin
        sys.stdout = old_stdout
        os.environ.clear()
        os.environ.update(old_env)


def test_bootstrap_atomic_publish_is_readable_by_real_builder_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real bootstrap publisher crosses the builder's completed-inode gate."""
    sealed_root = tmp_path / "application" / "python-runtime"
    sealed_root.mkdir(parents=True)
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    evidence = {
        "schema": BUILDER.ATTESTATION_SCHEMA,
        "nonce": "a" * 64,
        "role": "typed",
    }
    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    code = (
        "import json,pathlib,sys;"
        "sys.path.insert(0,sys.argv[1]);"
        "from tobkiri_sealed import bootstrap;"
        "bootstrap._publish_attestation(pathlib.Path(sys.argv[2]),json.loads(sys.argv[3]))"
    )
    with BUILDER._native_smoke_workspace(sealed_root) as workspace:
        directory = workspace.create_directory("attestation-typed")
        attestation = directory / f"startup-{'a' * 64}.json"
        assert not workspace.attestation_ready(attestation)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                code,
                str(source_root),
                str(attestation),
                json.dumps(evidence),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=_clean_sealed_test_environment(),
        )
        assert result.returncode == 0, result.stderr
        assert workspace.attestation_ready(attestation)
        assert workspace.read_attestation(attestation) == evidence
        assert attestation.stat().st_nlink == 1
        assert not list(directory.glob(".*.tmp"))


@pytest.mark.parametrize("case", ("precreate", "symlink", "hardlink"))
def test_bootstrap_atomic_publish_rejects_existing_destination(
    tmp_path: Path,
    case: str,
) -> None:
    """O_EXCL publication never replaces any pre-existing destination kind."""
    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    directory = tmp_path / "attestation"
    directory.mkdir(mode=0o700)
    attestation = directory / f"startup-{'d' * 64}.json"
    external = tmp_path / "external"
    external.write_text("external", encoding="utf-8")
    if case == "precreate":
        attestation.write_text("existing", encoding="utf-8")
    elif case == "symlink":
        attestation.symlink_to(external)
    else:
        os.link(external, attestation)
    code = (
        "import pathlib,sys;"
        "sys.path.insert(0,sys.argv[1]);"
        "from tobkiri_sealed import bootstrap;"
        "bootstrap._publish_attestation(pathlib.Path(sys.argv[2]),{'schema':'x'})"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(source_root), str(attestation)],
        check=False,
        capture_output=True,
        text=True,
        env=_clean_sealed_test_environment(),
    )
    assert result.returncode != 0
    assert "destination" in result.stderr
    assert attestation.read_text(encoding="utf-8") in {"existing", "external"}
    attestation.unlink()


@pytest.mark.parametrize("case", ("replace", "tamper"))
def test_builder_rejects_attestation_replacement_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """A same-shape pathname replacement cannot survive post-parse revalidation."""
    sealed_root = tmp_path / "application" / "python-runtime"
    sealed_root.mkdir(parents=True)
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    original_loads = BUILDER.json.loads
    with BUILDER._native_smoke_workspace(sealed_root) as workspace:
        directory = workspace.create_directory("attestation-typed")
        attestation = directory / f"startup-{'b' * 64}.json"
        attestation.write_text('{"schema":"original"}', encoding="utf-8")
        attestation.chmod(0o600)

        def replace_after_parse(payload: object) -> object:
            document = original_loads(payload)
            if case == "replace":
                displaced = directory / "displaced.json"
                attestation.rename(displaced)
                attestation.write_text('{"schema":"replacement"}', encoding="utf-8")
                attestation.chmod(0o600)
            else:
                attestation.write_text('{"schema":"tampered"}', encoding="utf-8")
            return document

        monkeypatch.setattr(BUILDER.json, "loads", replace_after_parse)
        with pytest.raises(
            BUILDER.SealedEnvironmentError,
            match="changed after validation",
        ):
            workspace.read_attestation(attestation)


@pytest.mark.parametrize("case", ("symlink", "hardlink", "mode"))
def test_builder_rejects_unpublished_attestation_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Linked or non-private output never becomes a completed publication."""
    sealed_root = tmp_path / "application" / "python-runtime"
    sealed_root.mkdir(parents=True)
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    external = tmp_path / "external-attestation"
    external.write_text('{"schema":"external"}', encoding="utf-8")
    external.chmod(0o600)
    with BUILDER._native_smoke_workspace(sealed_root) as workspace:
        directory = workspace.create_directory("attestation-typed")
        attestation = directory / f"startup-{'c' * 64}.json"
        if case == "symlink":
            attestation.symlink_to(external)
        elif case == "hardlink":
            os.link(external, attestation)
        else:
            attestation.write_text('{"schema":"mode"}', encoding="utf-8")
            attestation.chmod(0o644)
        if case == "hardlink":
            assert not workspace.attestation_ready(attestation)
        else:
            with pytest.raises(
                BUILDER.SealedEnvironmentError,
                match="identity is invalid",
            ):
                workspace.attestation_ready(attestation)
        with pytest.raises(
            BUILDER.SealedEnvironmentError,
            match="identity is invalid",
        ):
            workspace.read_attestation(attestation)
        attestation.unlink()


@pytest.mark.parametrize(
    "case",
    ("tamper", "stale-outer", "missing-resource", "second-overlay", "wrong-path", "hardlink"),
)
def test_bootstrap_rejects_runtime_overlay_drift(tmp_path: Path, case: str) -> None:
    """The Host overlay remains one exact, separately bound digest domain."""
    output = _fixture_sources(tmp_path / "sealed", "x86_64-unknown-linux-gnu")[2]
    overlay_digest, outer_digest = _install_runtime_overlay(output)
    manifest = json.loads((output / BUILDER.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    old_path = sys.path[:]
    old_bootstrap = sys.modules.pop("tobkiri_sealed.bootstrap", None)
    old_package = sys.modules.pop("tobkiri_sealed", None)
    try:
        sys.path.insert(0, str(source_root))
        import tobkiri_sealed.bootstrap as bootstrap

        binding = bootstrap._verify_runtime_overlay(output, manifest, overlay_digest, outer_digest)
        assert binding["runtime_overlay_sha256"] == overlay_digest
        app = output / "app"
        overlay = app / "runtime-resource-manifest.v1.json"
        app.chmod(0o755)
        overlay.chmod(0o644)
        if case == "tamper":
            overlay.write_bytes(overlay.read_bytes() + b"\n")
        elif case in {"stale-outer", "missing-resource"}:
            document = json.loads(overlay.read_text(encoding="utf-8"))
            if case == "stale-outer":
                document["overlay"]["outer_manifest_sha256"] = "e" * 64
            else:
                document["entries"][0]["path"] = "outer-only/missing.json"
            payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
            overlay.write_bytes(payload)
            overlay_digest = hashlib.sha256(payload).hexdigest()
        elif case == "second-overlay":
            (app / "unexpected-overlay.json").write_bytes(b"{}")
        elif case == "wrong-path":
            overlay.rename(app / "wrong-runtime-resource-manifest.v1.json")
        else:
            payload = overlay.read_bytes()
            overlay.unlink()
            authority = tmp_path / "overlay-authority"
            authority.write_bytes(payload)
            os.link(authority, overlay)
        for path in app.iterdir():
            if path.is_file():
                path.chmod(0o444)
        app.chmod(0o555)
        with pytest.raises(bootstrap.SealedBootstrapError):
            if case == "second-overlay":
                bootstrap._verify_tree(output, manifest)
            else:
                bootstrap._verify_runtime_overlay(output, manifest, overlay_digest, outer_digest)
    finally:
        sys.path = old_path
        sys.modules.pop("tobkiri_sealed.bootstrap", None)
        sys.modules.pop("tobkiri_sealed", None)
        if old_package is not None:
            sys.modules["tobkiri_sealed"] = old_package
        if old_bootstrap is not None:
            sys.modules["tobkiri_sealed.bootstrap"] = old_bootstrap
        sys.modules.pop("tobkiri_sealed.bootstrap", None)
        sys.modules.pop("tobkiri_sealed", None)


def test_bootstrap_sys_path_is_exact_manifest_bound_import_set(tmp_path: Path) -> None:
    """Only fixed inventory roots enter the attested isolated import set."""
    output = _fixture_sources(tmp_path / "sealed", "x86_64-unknown-linux-gnu")[2]
    document = json.loads((output / BUILDER.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    old_path = sys.path[:]
    old_package = sys.modules.pop("tobkiri_sealed", None)
    old_bootstrap = sys.modules.pop("tobkiri_sealed.bootstrap", None)
    try:
        sys.path.insert(0, str(source_root))
        import tobkiri_sealed.bootstrap as bootstrap

        expected = _fixture_sys_path(output)
        sys.path = _fixture_sys_path(output, include_missing_zip=True)
        assert (
            bootstrap._normalize_sys_path(
                output,
                document,
                include_application=False,
            )
            == expected
        )
        assert sys.path == expected

        sys.path = [str(output / "app"), *expected]
        assert set(
            bootstrap._normalize_sys_path(
                output,
                document,
                include_application=True,
            )
        ) == set(sys.path)

        defaultspack_import_root = "app/ecosystem/defaultspack"
        sys.path = [
            str(output / defaultspack_import_root),
            str(output / "app"),
            *expected,
        ]
        assert set(
            bootstrap._normalize_sys_path(
                output,
                document,
                include_application=True,
                application_import_roots=(defaultspack_import_root,),
            )
        ) == set(sys.path)
        with pytest.raises(
            bootstrap.SealedBootstrapError,
            match="role import roots require",
        ):
            bootstrap._normalize_sys_path(
                output,
                document,
                include_application=False,
                application_import_roots=(defaultspack_import_root,),
            )

        external = tmp_path / "external"
        external.mkdir()
        external_zip = external / "python313.zip"
        external_zip.write_bytes(b"external")
        user_site = external / "user-site/lib/python3.13/site-packages"
        user_site.mkdir(parents=True)
        lookalike = tmp_path / (output.name + "-lookalike")
        lookalike.mkdir()
        cwd = Path.cwd()
        for injected in (
            str(external_zip),
            str(output / "runtime/lib"),
            str(output / "lease.v1"),
            str(lookalike),
            "",
            ".",
            str(cwd),
            str(user_site),
        ):
            sys.path = [*expected, injected]
            with pytest.raises(bootstrap.SealedBootstrapError):
                bootstrap._normalize_sys_path(
                    output,
                    document,
                    include_application=False,
                )

        sys.path = [*expected, expected[0]]
        with pytest.raises(bootstrap.SealedBootstrapError, match="duplicate"):
            bootstrap._normalize_sys_path(
                output,
                document,
                include_application=False,
            )

        manifested = tmp_path / "manifested-zip"
        shutil.copytree(output, manifested)
        for directory in (manifested, manifested / "runtime", manifested / "runtime/lib"):
            _make_test_mutable(directory)
        zip_path = manifested / "runtime/lib/python313.zip"
        zip_path.write_bytes(b"PK\x05\x06" + b"\0" * 18)
        zip_path.chmod(0o444)
        (manifested / "runtime/lib").chmod(0o555)
        (manifested / "runtime").chmod(0o555)
        manifested.chmod(0o555)
        manifested_document = json.loads(json.dumps(document))
        manifested_document["files"].append(
            {
                "path": "runtime/lib/python313.zip",
                "size": zip_path.stat().st_size,
                "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
                "executable": False,
            }
        )
        manifested_document["files"].sort(key=lambda entry: entry["path"])
        manifested_expected = [
            str(zip_path),
            *_fixture_sys_path(manifested),
        ]
        sys.path = manifested_expected[:]
        assert (
            bootstrap._normalize_sys_path(
                manifested,
                manifested_document,
                include_application=False,
            )
            == manifested_expected
        )

        for directory in (output, output / "runtime", output / "runtime/lib"):
            _make_test_mutable(directory)
        (output / "runtime/lib/python313.zip").symlink_to(external_zip)
        (output / "runtime/lib").chmod(0o555)
        (output / "runtime").chmod(0o555)
        output.chmod(0o555)
        sys.path = _fixture_sys_path(output, include_missing_zip=True)
        with pytest.raises(bootstrap.SealedBootstrapError):
            bootstrap._normalize_sys_path(
                output,
                document,
                include_application=False,
            )
    finally:
        sys.path = old_path
        sys.modules.pop("tobkiri_sealed.bootstrap", None)
        sys.modules.pop("tobkiri_sealed", None)
        if old_package is not None:
            sys.modules["tobkiri_sealed"] = old_package
        if old_bootstrap is not None:
            sys.modules["tobkiri_sealed.bootstrap"] = old_bootstrap


def test_bootstrap_rejects_path_environment_and_external_import_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject inherited path variables, cwd, user-site, and shadow roots."""
    output = _fixture_sources(tmp_path / "sealed", "x86_64-unknown-linux-gnu")[2]
    overlay_arguments = _runtime_overlay_arguments(output)
    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    external = tmp_path / "external-shadow"
    external.mkdir()
    for filename in ("sitecustomize.py", "usercustomize.py", "shadow.pth"):
        (external / filename).write_text(
            "raise RuntimeError('external metadata executed')\n",
            encoding="utf-8",
        )
    (external / "app.py").write_text(
        "raise RuntimeError('external shadow imported')\n",
        encoding="utf-8",
    )
    attestation_dir = tmp_path / "attestation"
    attestation_dir.mkdir()
    nonce = "c" * 64

    old_path = sys.path[:]
    old_prefix = sys.prefix
    old_base_prefix = sys.base_prefix
    old_executable = sys.executable
    sys.modules.pop("tobkiri_sealed.bootstrap", None)
    sys.modules.pop("tobkiri_sealed", None)
    forbidden = (
        "REPO",
        "RUMI_CORE_DIR",
        "PYTHONPATH",
        "PYTHONHOME",
        "DYLD_LIBRARY_PATH",
        "LD_LIBRARY_PATH",
    )
    try:
        sys.path = _fixture_sys_path(output)
        sys.prefix = str(output / "venv")
        sys.base_prefix = str(output / "runtime")
        sys.executable = str(output / "venv/bin/python3")
        for key in forbidden:
            monkeypatch.delenv(key, raising=False)
        sys.path.insert(0, str(source_root))
        import tobkiri_sealed.bootstrap as bootstrap

        for key in forbidden:
            monkeypatch.setenv(key, str(external))
            attestation = attestation_dir / f"startup-{nonce}.json"
            with pytest.raises(bootstrap.SealedBootstrapError, match="forbidden"):
                bootstrap.main(
                    [
                        "--role",
                        "defaultspack",
                        "--nonce",
                        nonce,
                        "--attestation",
                        str(attestation),
                        "--manifest",
                        str(output / BUILDER.MANIFEST_FILENAME),
                        "--environment-root",
                        str(output),
                        *overlay_arguments,
                        "--",
                    ]
                )
            assert not attestation.exists()
            monkeypatch.delenv(key, raising=False)

        sys.path = [*_fixture_sys_path(output), str(external)]
        monkeypatch.chdir(external)
        attestation = attestation_dir / f"startup-{nonce}.json"
        with pytest.raises(bootstrap.SealedBootstrapError, match="escaped"):
            bootstrap.main(
                [
                    "--role",
                    "defaultspack",
                    "--nonce",
                    nonce,
                    "--attestation",
                    str(attestation),
                    "--manifest",
                    str(output / BUILDER.MANIFEST_FILENAME),
                    "--environment-root",
                    str(output),
                    *overlay_arguments,
                    "--",
                ]
            )
        assert not attestation.exists()
    finally:
        sys.path = old_path
        sys.prefix = old_prefix
        sys.base_prefix = old_base_prefix
        sys.executable = old_executable
        sys.modules.pop("tobkiri_sealed.bootstrap", None)
        sys.modules.pop("tobkiri_sealed", None)


def test_fresh_isolated_subprocess_rejects_external_launch_metadata(
    tmp_path: Path,
) -> None:
    """A real isolated interpreter starts only from the sealed snapshot."""
    target = "x86_64-pc-windows-msvc" if os.name == "nt" else "x86_64-unknown-linux-gnu"
    output = _fixture_sources(tmp_path / "sealed", target)[2]
    overlay_digest, outer_digest = _install_runtime_overlay(output)
    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    external = tmp_path / "external-shadow"
    external.mkdir()
    metadata_code = (
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['METADATA_MARKER']).write_text('executed')\n"
    )
    for filename in ("sitecustomize.py", "usercustomize.py", "shadow.pth"):
        (external / filename).write_text(metadata_code, encoding="utf-8")
    (external / "app.py").write_text(metadata_code, encoding="utf-8")
    user_site = external / "user-site/lib/python3.13/site-packages"
    user_site.mkdir(parents=True)
    for filename in ("sitecustomize.py", "usercustomize.py", "user-shadow.pth"):
        (user_site / filename).write_text(metadata_code, encoding="utf-8")

    child_code = """
import json
import os
import sys
from pathlib import Path

source_root = Path(sys.argv[1])
output = Path(sys.argv[2])
attestation = Path(sys.argv[3])
if os.name == "nt":
    site_packages = output / "venv/Lib/site-packages"
    stdlib = output / "runtime/Lib"
    dynload = output / "runtime/DLLs"
    runtime_zip = output / "runtime/python313.zip"
    runtime_root = output / "runtime"
    executable = output / "venv/Scripts/python.exe"
else:
    import fcntl
    site_packages = output / "venv/lib/python3.13/site-packages"
    stdlib = output / "runtime/lib/python3.13"
    dynload = stdlib / "lib-dynload"
    runtime_zip = output / "runtime/lib/python313.zip"
    runtime_root = None
    executable = output / "venv/bin/python3"
sys.path.insert(0, str(source_root))
import tobkiri_sealed.bootstrap as bootstrap

sealed_path = [
    str(runtime_zip),
    *([str(runtime_root)] if runtime_root is not None else []),
    str(site_packages),
    str(stdlib),
    str(dynload),
]
if os.environ.get("INJECT_EXTERNAL_PATH"):
    sys.path = [os.environ["INJECT_EXTERNAL_PATH"], *sealed_path]
else:
    sys.path = sealed_path
sys.prefix = str(output / "venv")
sys.base_prefix = str(output / "runtime")
sys.executable = str(executable)
sys.dont_write_bytecode = True
result = bootstrap.main(
    [
        "--role",
        "defaultspack",
        "--nonce",
        "d" * 64,
        "--attestation",
        str(attestation),
        "--manifest",
        str(output / "sealed-environment.v1.json"),
        "--environment-root",
        str(output),
        "--runtime-overlay-sha256",
        os.environ["RUNTIME_OVERLAY_SHA256"],
        "--outer-runtime-manifest-sha256",
        os.environ["OUTER_RUNTIME_MANIFEST_SHA256"],
        "--application-bundle-root",
        "",
        "--packvm-provisioning-sha256",
        "",
        "--packvm-helper-manifest-sha256",
        "",
        "--packvm-helper-team-id",
        "",
        "--",
        "--subprocess",
    ]
)
print(json.dumps({"result": result, "sys_path": list(sys.path)}))
"""
    base_env = _clean_sealed_test_environment()
    base_env["ROLE_MARKER"] = str(tmp_path / "roles.jsonl")
    base_env["TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256"] = BUILDER._sha256_file(
        output / BUILDER.MANIFEST_FILENAME
    )
    base_env["RUNTIME_OVERLAY_SHA256"] = overlay_digest
    base_env["OUTER_RUNTIME_MANIFEST_SHA256"] = outer_digest
    base_env["METADATA_MARKER"] = str(tmp_path / "metadata-success")
    attestation_name = "startup-" + "d" * 64 + ".json"
    success_attestation = tmp_path / "attestation-success" / attestation_name
    success_attestation.parent.mkdir()
    success = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            child_code,
            str(source_root),
            str(output),
            str(success_attestation),
        ],
        cwd=external,
        env=base_env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert success.returncode == 0, success.stderr
    child_evidence = json.loads(success.stdout)
    attestation_evidence = json.loads(success_attestation.read_text(encoding="utf-8"))
    assert child_evidence["result"] == 8
    assert child_evidence["sys_path"] == attestation_evidence["sys_path"]
    assert not Path(base_env["METADATA_MARKER"]).exists()
    assert success_attestation.is_file()

    for index, key in enumerate(
        (
            "REPO",
            "RUMI_CORE_DIR",
            "PYTHONPATH",
            "PYTHONHOME",
            "DYLD_LIBRARY_PATH",
            "LD_LIBRARY_PATH",
        ),
        start=1,
    ):
        environment = base_env.copy()
        environment[key] = str(external)
        environment["METADATA_MARKER"] = str(tmp_path / f"metadata-{index}")
        attestation = tmp_path / f"attestation-{index}" / attestation_name
        attestation.parent.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                child_code,
                str(source_root),
                str(output),
                str(attestation),
            ],
            cwd=external,
            env=environment,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode != 0
        assert "forbidden injection keys" in result.stderr
        assert not attestation.exists()
        assert not Path(environment["METADATA_MARKER"]).exists()

    environment = base_env.copy()
    environment["INJECT_EXTERNAL_PATH"] = str(external)
    environment["METADATA_MARKER"] = str(tmp_path / "metadata-path")
    attestation = tmp_path / "attestation-path" / attestation_name
    attestation.parent.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            child_code,
            str(source_root),
            str(output),
            str(attestation),
        ],
        cwd=external,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode != 0
    assert not attestation.exists()
    assert not Path(environment["METADATA_MARKER"]).exists()


@pytest.mark.parametrize(
    ("target", "output"),
    (
        (
            "aarch64-apple-darwin",
            "uv 0.11.14 (3fdfdc7d4 2026-05-12 aarch64-apple-darwin)\n",
        ),
        (
            "x86_64-apple-darwin",
            "uv 0.11.14 (3fdfdc7d4 2026-05-12 x86_64-apple-darwin)\n",
        ),
        (
            "x86_64-unknown-linux-gnu",
            "uv 0.11.14 (3fdfdc7d4 2026-05-12 x86_64-unknown-linux-gnu)\n",
        ),
        (
            "x86_64-pc-windows-msvc",
            "uv 0.11.14 (3fdfdc7d4 2026-05-12 x86_64-pc-windows-msvc)\n",
        ),
    ),
)
def test_uv_parser_accepts_official_structured_output(
    target: str,
    output: str,
) -> None:
    """Official uv 0.11.14 output binds version and executable identity."""
    identity = BUILDER.parse_uv_version(output, expected_target=target)
    assert identity.version == BUILDER.UV_VERSION
    assert identity.revision == "3fdfdc7d4"
    assert identity.release_date == "2026-05-12"
    assert identity.target == target


@pytest.mark.parametrize(
    ("output", "expected_target"),
    (
        (
            "uv 0.11.13 (3fdfdc7d4 2026-05-12 aarch64-apple-darwin)\n",
            "aarch64-apple-darwin",
        ),
        (
            "uv 0.11.14 (3fdfdc7d4 2026-05-12 x86_64-apple-darwin)\n",
            "aarch64-apple-darwin",
        ),
        (
            "warning: uv 0.11.14 (3fdfdc7d4 2026-05-12 aarch64-apple-darwin)\n",
            "aarch64-apple-darwin",
        ),
        ("uv 0.11.14\n", "aarch64-apple-darwin"),
        (
            "uv 0.11.14 (3FDFDC7D4 2026-05-12 aarch64-apple-darwin)\n",
            "aarch64-apple-darwin",
        ),
        (
            "uv 0.11.14 (3fdfdc7d4 2026-5-12 aarch64-apple-darwin)\n",
            "aarch64-apple-darwin",
        ),
        (
            "uv 0.11.14 (3fdfdc7d4 2026-05-12 aarch64-apple-darwin extra)\n",
            "aarch64-apple-darwin",
        ),
        (
            "uv 0.11.14 (3fdfdc7d4 2026-05-12 arm64-apple-darwin)\n",
            "aarch64-apple-darwin",
        ),
    ),
)
def test_uv_parser_rejects_version_prefix_tamper_and_wrong_binary(
    output: str,
    expected_target: str,
) -> None:
    """Unexpected display text, version, metadata, or target fails closed."""
    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER.parse_uv_version(output, expected_target=expected_target)


def test_uv_version_runner_uses_structured_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generator checks the executable-reported target, not just text."""
    output = "uv 0.11.14 (3fdfdc7d4 2026-05-12 aarch64-apple-darwin)\n"
    monkeypatch.setattr(
        BUILDER.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(stdout=output),
    )
    identity = BUILDER._uv_version(Path("uv"), "aarch64-apple-darwin")
    assert identity.target == "aarch64-apple-darwin"
    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER._uv_version(Path("wrong-architecture-uv"), "x86_64-apple-darwin")


def test_extracted_python_identity_forces_no_bytecode_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extracted runtime receives -B and cannot inherit bytecode writes."""
    spec = BUILDER.target_spec("x86_64-unknown-linux-gnu")
    runtime = tmp_path / "runtime"
    python = runtime / "bin/python3"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic python")
    python.chmod(0o755)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> types.SimpleNamespace:
        calls.append((command, kwargs))
        return types.SimpleNamespace(
            stdout=json.dumps({"version": BUILDER.PYTHON_VERSION, "machine": "x86_64"})
        )

    monkeypatch.setenv(BUILDER.PYTHON_BYTECODE_ENVIRONMENT, "0")
    monkeypatch.setattr(BUILDER.subprocess, "run", fake_run)

    assert BUILDER._find_runtime(runtime, spec) == runtime
    command, kwargs = calls[0]
    assert command[:3] == [str(python), "-I", "-B"]
    assert kwargs["env"][BUILDER.PYTHON_BYTECODE_ENVIRONMENT] == "1"


@pytest.mark.parametrize("candidate_name", ("python3.13", "python"))
def test_uv_venv_python_alias_is_materialized_to_required_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_name: str,
) -> None:
    """A supported uv alias becomes the regular formal venv/bin/python3."""
    runtime = tmp_path / "runtime"
    runtime_python = runtime / "bin/python3.13"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"verified candidate interpreter")
    runtime_python.chmod(0o755)
    venv = tmp_path / "venv"
    candidate = venv / "bin" / candidate_name
    candidate.parent.mkdir(parents=True)
    candidate.symlink_to(Path("../../runtime/bin/python3.13"))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> types.SimpleNamespace:
        calls.append((command, kwargs))
        invoked = Path(command[0]).resolve()
        return types.SimpleNamespace(
            stdout=json.dumps(
                {
                    "version": BUILDER.PYTHON_VERSION,
                    "executable": str(invoked),
                    "prefix": str(venv),
                    "base_prefix": str(runtime),
                }
            ),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(BUILDER.subprocess, "run", fake_run)
    result = BUILDER._normalize_venv_python(
        venv,
        runtime,
        BUILDER.target_spec("aarch64-apple-darwin"),
    )

    assert result == venv / "bin/python3"
    assert result.read_bytes() == runtime_python.read_bytes()
    assert result.is_file() and not result.is_symlink()
    assert result.stat().st_nlink == 1
    assert all(command[1:3] == ["-I", "-B"] for command, _ in calls)
    assert all(kwargs["env"][BUILDER.PYTHON_BYTECODE_ENVIRONMENT] == "1" for _, kwargs in calls)


def test_uv_venv_python_alias_rejects_ambiguous_regular_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different candidate files never win a path-based interpreter race."""
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    venv_bin = tmp_path / "venv/bin"
    venv_bin.mkdir(parents=True)
    for name, payload in (("python3.13", b"one"), ("python", b"two")):
        path = venv_bin / name
        path.write_bytes(payload)
        path.chmod(0o755)
    monkeypatch.setattr(
        BUILDER.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("ambiguous candidates must fail before execution"),
    )

    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="ambiguous venv interpreter candidates",
    ):
        BUILDER._normalize_venv_python(
            tmp_path / "venv",
            runtime,
            BUILDER.target_spec("aarch64-apple-darwin"),
        )


def test_uv_venv_python_alias_rejects_missing_candidates(tmp_path: Path) -> None:
    """A missing formal path and every supported alias fail closed."""
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (tmp_path / "venv/bin").mkdir(parents=True)

    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="no supported interpreter candidate",
    ):
        BUILDER._normalize_venv_python(
            tmp_path / "venv",
            runtime,
            BUILDER.target_spec("aarch64-apple-darwin"),
        )


def test_uv_venv_python_alias_rejects_external_symlink(tmp_path: Path) -> None:
    """A known alias pointing outside the private build roots is rejected."""
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    venv_bin = tmp_path / "venv/bin"
    venv_bin.mkdir(parents=True)
    outside = tmp_path / "outside-python"
    outside.write_bytes(b"outside")
    outside.chmod(0o755)
    (venv_bin / "python3.13").symlink_to(outside)

    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="(outside the venv/runtime roots|candidate link is not safe)",
    ):
        BUILDER._normalize_venv_python(
            tmp_path / "venv",
            runtime,
            BUILDER.target_spec("aarch64-apple-darwin"),
        )


def test_uv_venv_python_alias_rejects_path_swap_during_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate replaced during identity validation cannot be materialized."""
    runtime = tmp_path / "runtime"
    runtime_python = runtime / "bin/python3.13"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"verified candidate")
    runtime_python.chmod(0o755)
    venv = tmp_path / "venv"
    candidate = venv / "bin/python3.13"
    candidate.parent.mkdir(parents=True)
    candidate.symlink_to(Path("../../runtime/bin/python3.13"))
    outside = tmp_path / "outside-python"
    outside.write_bytes(b"outside")
    outside.chmod(0o755)

    def fake_run(command: list[str], **_kwargs: object) -> types.SimpleNamespace:
        del command
        candidate.unlink()
        candidate.symlink_to(outside)
        return types.SimpleNamespace(
            stdout=json.dumps(
                {
                    "version": BUILDER.PYTHON_VERSION,
                    "executable": str(runtime_python),
                    "prefix": str(venv),
                    "base_prefix": str(runtime),
                }
            ),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(BUILDER.subprocess, "run", fake_run)
    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="(outside the venv/runtime roots|candidate link is not safe)",
    ):
        BUILDER._normalize_venv_python(
            venv,
            runtime,
            BUILDER.target_spec("aarch64-apple-darwin"),
        )


def test_uv_venv_python_alias_rejects_wrong_interpreter_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runnable candidate with the wrong version is not accepted."""
    runtime = tmp_path / "runtime"
    runtime_python = runtime / "bin/python3.13"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"wrong interpreter")
    runtime_python.chmod(0o755)
    venv = tmp_path / "venv"
    candidate = venv / "bin/python3.13"
    candidate.parent.mkdir(parents=True)
    candidate.symlink_to(Path("../../runtime/bin/python3.13"))

    monkeypatch.setattr(
        BUILDER.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            stdout=json.dumps(
                {
                    "version": "3.12.0",
                    "executable": str(runtime_python),
                    "prefix": str(venv),
                    "base_prefix": str(runtime),
                }
            ),
            stderr="",
            returncode=0,
        ),
    )
    with pytest.raises(
        BUILDER.SealedEnvironmentError,
        match="wrong Python version",
    ):
        BUILDER._normalize_venv_python(
            venv,
            runtime,
            BUILDER.target_spec("aarch64-apple-darwin"),
        )


def test_uv_runner_preserves_clear_environment_and_disables_bytecode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uv's clear environment also protects Python used during venv setup."""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> None:
        calls.append((command, kwargs))

    monkeypatch.setattr(BUILDER.subprocess, "run", fake_run)
    cache = tmp_path / "cache"
    cache.mkdir()
    BUILDER._run_uv(
        tmp_path / "uv",
        ["venv", tmp_path / "venv"],
        tmp_path,
        cache,
    )

    command, kwargs = calls[0]
    assert command[0] == str(tmp_path / "uv")
    environment = kwargs["env"]
    assert environment[BUILDER.PYTHON_BYTECODE_ENVIRONMENT] == "1"
    assert "PYTHONPATH" not in environment
    assert environment["UV_NO_CONFIG"] == "1"


def test_role_smoke_forces_no_bytecode_environment_and_B(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role child cannot re-enable bytecode through its inherited env."""
    spec = BUILDER.target_spec("x86_64-unknown-linux-gnu")
    root = tmp_path / "application" / "sealed"
    python = root / "venv/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic python")
    python.chmod(0o755)
    runtime_overlay_sha256 = "e" * 64
    outer_runtime_manifest_sha256 = "f" * 64
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> types.SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = kwargs
        nonce = command[command.index("--nonce") + 1]
        attestation = Path(command[command.index("--attestation") + 1])
        attestation.write_text(
            json.dumps(
                {
                    "schema": BUILDER.ATTESTATION_SCHEMA,
                    "nonce": nonce,
                    "role": "host_helper",
                    "environment_digest": "a" * 64,
                    "executable": str(python),
                    "prefix": str(root / "venv"),
                    "base_prefix": str(root / "runtime"),
                    "sys_path": [],
                    "stdlib_sha256": "b" * 64,
                    "site_packages_sha256": "c" * 64,
                    "native_sha256": "d" * 64,
                    "runtime_overlay_sha256": runtime_overlay_sha256,
                    "outer_runtime_manifest_sha256": outer_runtime_manifest_sha256,
                    "lifetime_lease": True,
                }
            ),
            encoding="utf-8",
        )
        attestation.chmod(0o600)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    environment = {BUILDER.PYTHON_BYTECODE_ENVIRONMENT: "0"}
    monkeypatch.setattr(BUILDER.subprocess, "run", fake_run)
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    before = tuple(
        (path.relative_to(root).as_posix(), path.lstat().st_mode, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    )
    with BUILDER._native_smoke_workspace(root) as workspace:
        workspace_path = workspace.path
        BUILDER._run_role_smoke(
            root,
            spec,
            "host_helper",
            (),
            environment,
            workspace,
            "a" * 64,
            runtime_overlay_sha256,
            outer_runtime_manifest_sha256,
        )
        assert workspace.path.is_relative_to(temp_parent)
        assert not workspace.path.is_relative_to(root.parent)
    after = tuple(
        (path.relative_to(root).as_posix(), path.lstat().st_mode, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    )

    command = captured["command"]
    kwargs = captured["kwargs"]
    assert command[:3] == [str(python), "-I", "-B"]
    for option in (
        "--application-bundle-root",
        "--packvm-provisioning-sha256",
        "--packvm-helper-manifest-sha256",
        "--packvm-helper-team-id",
    ):
        assert command[command.index(option) + 1] == ""
    assert kwargs["env"][BUILDER.PYTHON_BYTECODE_ENVIRONMENT] == "1"
    assert environment[BUILDER.PYTHON_BYTECODE_ENVIRONMENT] == "0"
    assert after == before
    assert not workspace_path.exists()
    assert all(
        BUILDER.SMOKE_WORKSPACE_PREFIX not in value
        for key, value in kwargs["env"].items()
        if key != "TMPDIR"
    )
    with BUILDER._native_smoke_workspace(root) as workspace:
        with pytest.raises(BUILDER.SealedEnvironmentError, match="identity is invalid"):
            BUILDER._run_role_smoke(
                root,
                spec,
                "host_helper",
                (),
                environment,
                workspace,
                "f" * 64,
                runtime_overlay_sha256,
                outer_runtime_manifest_sha256,
            )


def test_native_smoke_workspace_rejects_symlinked_temp_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attacker-controlled TMPDIR alias cannot redirect Host smoke state."""
    real = tmp_path / "real-temp"
    real.mkdir()
    alias = tmp_path / "alias-temp"
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("TMPDIR", str(alias))

    with pytest.raises(BUILDER.SealedEnvironmentError, match="contains a symlink"):
        BUILDER._NativeSmokeWorkspace(tmp_path / "app" / "python-runtime")


def test_native_smoke_environment_excludes_host_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke children receive fixed state paths, never ambient CI credentials."""
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    for key in (
        "GITHUB_TOKEN",
        "TOBKIRI_PACKAGING_TRANSACTION_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, f"secret-{key}")
    sealed_root = tmp_path / "app" / "python-runtime"
    sealed_root.mkdir(parents=True)
    (sealed_root / BUILDER.MANIFEST_FILENAME).write_text("manifest", encoding="utf-8")
    with BUILDER._native_smoke_workspace(sealed_root) as workspace:
        environment = BUILDER._native_smoke_environment(
            sealed_root,
            BUILDER.target_spec("aarch64-apple-darwin"),
            workspace,
        )
        assert all(
            key not in environment
            for key in (
                "GITHUB_TOKEN",
                "TOBKIRI_PACKAGING_TRANSACTION_TOKEN",
                "AWS_SECRET_ACCESS_KEY",
                "PYTHONPATH",
            )
        )
        for key in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
            assert Path(environment[key]).is_relative_to(workspace.path)


def test_native_smoke_uses_separate_single_file_runtime_overlay(tmp_path: Path) -> None:
    """The build-script smoke path copies a valid base before adding its overlay."""
    target = "x86_64-unknown-linux-gnu"
    output = _fixture_sources(tmp_path / "sealed", target)[2]
    document = json.loads((output / BUILDER.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert not (output / "app/runtime-resource-manifest.v1.json").exists()
    BUILDER.validate_environment(output, target, run_native_smoke=False)
    with BUILDER._native_smoke_workspace(output) as workspace:
        snapshot, overlay_digest, outer_digest = BUILDER._create_native_smoke_runtime_snapshot(
            output,
            BUILDER.target_spec(target),
            workspace,
            document,
        )
        overlay = snapshot / "app/runtime-resource-manifest.v1.json"
        payload = overlay.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == overlay_digest
        overlay_document = json.loads(payload)
        assert overlay_document["overlay"]["outer_manifest_sha256"] == outer_digest
        assert [
            path.relative_to(snapshot).as_posix()
            for path in snapshot.rglob("runtime-resource-manifest.v1.json")
        ] == ["app/runtime-resource-manifest.v1.json"]
        with pytest.raises(BUILDER.SealedEnvironmentError, match="inventory"):
            BUILDER.validate_environment(snapshot, target, run_native_smoke=False)
        BUILDER._thaw_native_smoke_runtime_snapshot(snapshot, workspace)
    BUILDER.validate_environment(output, target, run_native_smoke=False)


def test_native_smoke_separates_runtime_snapshot_from_host_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The packaging smoke uses the same disjoint Host launch layout as runtime."""
    target = "x86_64-unknown-linux-gnu"
    spec = BUILDER.target_spec(target)
    output = _fixture_sources(tmp_path / "sealed", target)[2]
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    monkeypatch.setattr(BUILDER, "_native_host_spec", lambda: spec)
    monkeypatch.setattr(BUILDER, "_run_native_import_smoke", lambda *_args: None)
    observed_workspaces: list[tuple[Path, Path]] = []

    def fake_role_smoke(
        runtime_root: Path,
        _spec: object,
        _role: str,
        _role_arguments: object,
        environment: dict[str, str],
        workspace: object,
        _environment_digest: str,
        _overlay_digest: str,
        _outer_digest: str,
    ) -> None:
        host_root = workspace.path.resolve(strict=True)
        snapshot_root = runtime_root.resolve(strict=True)
        assert not host_root.is_relative_to(snapshot_root)
        assert not host_root.is_relative_to(snapshot_root.parent)
        for key in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
            assert Path(environment[key]).resolve(strict=True).is_relative_to(host_root)
        observed_workspaces.append((snapshot_root.parent, host_root))

    monkeypatch.setattr(BUILDER, "_run_role_smoke", fake_role_smoke)
    BUILDER._verify_python_smoke(output, spec)

    assert len(observed_workspaces) == 3
    runtime_workspace, host_workspace = observed_workspaces[0]
    assert runtime_workspace != host_workspace
    assert all(pair == observed_workspaces[0] for pair in observed_workspaces)
    assert not runtime_workspace.exists()
    assert not host_workspace.exists()


def test_native_smoke_rejects_overlapping_runtime_and_host_workspaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller cannot collapse the typed runtime and Host-state domains."""
    target = "x86_64-unknown-linux-gnu"
    output = _fixture_sources(tmp_path / "sealed", target)[2]
    document = json.loads((output / BUILDER.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    with BUILDER._native_smoke_workspace(output) as workspace:
        with pytest.raises(
            BUILDER.SealedEnvironmentError,
            match="runtime and Host workspaces overlap",
        ):
            BUILDER._verify_python_smoke_in_workspaces(
                output,
                BUILDER.target_spec(target),
                document,
                workspace,
                workspace,
            )


def test_native_smoke_workspace_rejects_root_swap_and_preserves_external_victim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace replacement cannot redirect verification or cleanup."""
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    victim = tmp_path / "external-victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    sealed_root = tmp_path / "app" / "python-runtime"
    sealed_root.mkdir(parents=True)
    workspace = BUILDER._NativeSmokeWorkspace(sealed_root)
    original = workspace.path.with_name(f"{workspace.path.name}.original")
    workspace.path.rename(original)
    workspace.path.symlink_to(victim, target_is_directory=True)
    try:
        with pytest.raises(BUILDER.SealedEnvironmentError, match="identity changed"):
            workspace.verify()
        assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"
    finally:
        workspace.path.unlink()
        original.rename(workspace.path)
        workspace.cleanup()


def test_native_smoke_workspace_rejects_precreated_child_and_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Precreated work paths and multiply-linked attestations fail closed."""
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    external = tmp_path / "external-victim"
    external.write_text("victim", encoding="utf-8")
    external.chmod(0o600)
    sealed_root = tmp_path / "app" / "python-runtime"
    sealed_root.mkdir(parents=True)
    workspace = BUILDER._NativeSmokeWorkspace(sealed_root)
    try:
        attestation_parent = workspace.create_directory("attestation-typed")
        with pytest.raises(BUILDER.SealedEnvironmentError, match="already exists"):
            workspace.create_directory("attestation-typed")
        attestation = attestation_parent / f"startup-{'a' * 64}.json"
        os.link(external, attestation)
        with pytest.raises(BUILDER.SealedEnvironmentError, match="identity is invalid"):
            workspace.read_attestation(attestation)
        with pytest.raises(RuntimeError, match="hard-linked file"):
            workspace.cleanup()
        assert not workspace.path.exists()
        workspace.cleanup()
    finally:
        if not workspace.removed:
            workspace.cleanup()
    assert external.read_text(encoding="utf-8") == "victim"


def test_native_smoke_workspace_rejects_wrong_device_and_cleanup_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A created root stays on its parent device and may be cleaned twice."""
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))
    metadata = temp_parent.lstat()
    wrong_parent = BUILDER._SmokePathIdentity(
        metadata.st_dev + 1,
        metadata.st_ino,
        metadata.st_uid,
    )
    with pytest.raises(BUILDER.SealedEnvironmentError, match="device boundary"):
        BUILDER._validate_smoke_workspace_metadata(metadata, wrong_parent)

    sealed_root = tmp_path / "app" / "python-runtime"
    sealed_root.mkdir(parents=True)
    workspace = BUILDER._NativeSmokeWorkspace(sealed_root)
    path = workspace.path
    workspace.cleanup()
    workspace.cleanup()
    assert not path.exists()


def test_native_smoke_child_error_precedes_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cleanup diagnostics never replace the native child failure status."""
    temp_parent = tmp_path / "host-temp"
    temp_parent.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temp_parent))

    def fail_cleanup(_workspace: object) -> None:
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(BUILDER._NativeSmokeWorkspace, "cleanup", fail_cleanup)
    sealed_root = tmp_path / "app" / "python-runtime"
    sealed_root.mkdir(parents=True)
    with pytest.raises(BUILDER.SealedEnvironmentError, match="child failed"):
        with BUILDER._native_smoke_workspace(sealed_root):
            raise BUILDER.SealedEnvironmentError("child failed")
    assert "cleanup also failed" in capsys.readouterr().err


def test_bootstrap_rejects_attestation_inside_application_resources(
    tmp_path: Path,
) -> None:
    """The child refuses attestation output beside its sealed environment."""
    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    old_path = sys.path[:]
    old_bootstrap = sys.modules.pop("tobkiri_sealed.bootstrap", None)
    old_package = sys.modules.pop("tobkiri_sealed", None)
    try:
        sys.path.insert(0, str(source_root))
        import tobkiri_sealed.bootstrap as bootstrap

        application_root = tmp_path / "Contents" / "Resources" / "app"
        sealed_root = application_root / "python-runtime"
        attestation_parent = application_root / "attestation"
        sealed_root.mkdir(parents=True)
        attestation_parent.mkdir()
        nonce = "a" * 64
        with pytest.raises(bootstrap.SealedBootstrapError, match="application resources"):
            bootstrap._attestation_destination(
                str(attestation_parent / f"startup-{nonce}.json"),
                sealed_root,
                nonce,
            )
    finally:
        sys.path = old_path
        sys.modules.pop("tobkiri_sealed.bootstrap", None)
        sys.modules.pop("tobkiri_sealed", None)
        if old_package is not None:
            sys.modules["tobkiri_sealed"] = old_package
        if old_bootstrap is not None:
            sys.modules["tobkiri_sealed.bootstrap"] = old_bootstrap


def test_no_bytecode_flag_wins_over_child_environment_override(tmp_path: Path) -> None:
    """A child changing its env after startup still cannot write bytecode."""
    module = tmp_path / "runtime_module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    code = (
        "import os, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "os.environ['PYTHONDONTWRITEBYTECODE'] = '0'; "
        "import runtime_module; "
        "print(sys.dont_write_bytecode)"
    )
    environment = _clean_sealed_test_environment()
    environment[BUILDER.PYTHON_BYTECODE_ENVIRONMENT] = "0"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(tmp_path)],
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
    assert not list(tmp_path.rglob("__pycache__"))


def _write_fake_uv(path: Path, output: str, *, mode: int = 0o555) -> None:
    """Write a tiny executable that can exercise the uv identity gate."""
    path.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{output.rstrip()}'\n",
        encoding="utf-8",
    )
    path.chmod(mode)


def test_builder_never_uses_fake_path_uv_when_bundled_binary_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PATH executable cannot satisfy the production pinned-uv contract."""
    repo_root = tmp_path / "repo"
    bundled = repo_root / "tobkiri_runtime" / "bundled"
    bundled.mkdir(parents=True)
    requirements = repo_root / "tobkiri_runtime" / "requirements.txt"
    requirements.write_text("", encoding="utf-8")
    fake_dir = tmp_path / "fake-bin"
    fake_dir.mkdir()
    marker = tmp_path / "executed"
    fake_uv = fake_dir / "uv"
    _write_fake_uv(
        fake_uv,
        "uv 0.11.14 (3fdfdc7d4 2026-05-12 x86_64-unknown-linux-gnu)",
    )
    fake_uv.chmod(0o755)
    fake_uv.write_text(
        fake_uv.read_text(encoding="utf-8") + f"touch '{marker}'\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o555)
    monkeypatch.setenv("PATH", str(fake_dir))

    with pytest.raises(BUILDER.SealedEnvironmentError, match="disappeared"):
        BUILDER.build_environment(
            repo_root,
            "x86_64-unknown-linux-gnu",
        )
    assert not marker.exists()


def test_builder_rejects_correct_version_stdout_spoof(
    tmp_path: Path,
) -> None:
    """A fake executable cannot pass by printing the pinned version."""
    expected_target = "x86_64-unknown-linux-gnu"
    repo_root = tmp_path / "repo"
    bundled = repo_root / "tobkiri_runtime" / "bundled"
    bundled.mkdir(parents=True)
    bundled.chmod(0o700)
    uv = bundled / "uv"
    _write_fake_uv(
        uv,
        "uv 0.11.14 (3fdfdc7d4 2026-05-12 x86_64-unknown-linux-gnu)",
    )
    with pytest.raises(BUILDER.SealedEnvironmentError, match="SHA256 mismatch"):
        BUILDER._validate_pinned_uv_executable(
            repo_root,
            uv,
            BUILDER.target_spec(expected_target),
        )


@pytest.mark.parametrize(
    "output",
    (
        "uv 0.11.13 (3fdfdc7d4 2026-05-12 x86_64-unknown-linux-gnu)",
        "uv 0.11.14 (3fdfdc7d4 2026-05-12 aarch64-apple-darwin)",
    ),
)
def test_builder_rejects_wrong_version_or_target_after_byte_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    """Even a byte-bound fixture must report the requested official identity."""
    expected_target = "x86_64-unknown-linux-gnu"
    repo_root = tmp_path / "repo"
    bundled = repo_root / "tobkiri_runtime" / "bundled"
    bundled.mkdir(parents=True)
    bundled.chmod(0o700)
    uv = bundled / "uv"
    _write_fake_uv(uv, output)
    monkeypatch.setitem(
        BUILDER.UV_BINARY_SHA256_BY_TARGET,
        expected_target,
        BUILDER._sha256_file(uv),
    )
    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER._validate_pinned_uv_executable(
            repo_root,
            uv,
            BUILDER.target_spec(expected_target),
        )


def test_builder_rejects_owner_writable_staged_uv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extracted executable is immutable before uv is ever invoked."""
    expected_target = "x86_64-unknown-linux-gnu"
    repo_root = tmp_path / "repo"
    bundled = repo_root / "tobkiri_runtime" / "bundled"
    bundled.mkdir(parents=True)
    bundled.chmod(0o700)
    uv = bundled / "uv"
    _write_fake_uv(
        uv,
        "uv 0.11.14 (3fdfdc7d4 2026-05-12 x86_64-unknown-linux-gnu)",
        mode=0o755,
    )
    monkeypatch.setitem(
        BUILDER.UV_BINARY_SHA256_BY_TARGET,
        expected_target,
        BUILDER._sha256_file(uv),
    )
    with pytest.raises(BUILDER.SealedEnvironmentError, match="owner-writable"):
        BUILDER._validate_pinned_uv_executable(
            repo_root,
            uv,
            BUILDER.target_spec(expected_target),
        )


def test_pinned_uv_archive_and_binary_maps_cover_every_supported_target() -> None:
    """Every release target has both immutable archive and member identities."""
    targets = set(BUILDER.TARGETS)
    assert set(BUILDER.UV_ARCHIVE_SHA256_BY_TARGET) == targets
    assert set(BUILDER.UV_BINARY_SHA256_BY_TARGET) == targets
    assert all(
        len(digest) == 64 and digest == digest.lower()
        for digest in (
            *BUILDER.UV_ARCHIVE_SHA256_BY_TARGET.values(),
            *BUILDER.UV_BINARY_SHA256_BY_TARGET.values(),
        )
    )


def test_pinned_uv_maps_match_the_resource_preparer() -> None:
    """The generator and resource stage cannot silently drift in their pins."""
    preparer_path = ROOT / ".github" / "scripts" / "prepare_tauri_resources.py"
    spec = importlib.util.spec_from_file_location("sealed_uv_preparer_tests", preparer_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {preparer_path}")
    preparer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = preparer
    spec.loader.exec_module(preparer)
    assert preparer.UV_PINNED_VERSION == BUILDER.UV_VERSION
    assert preparer.UV_SHA256_BY_TARGET == BUILDER.UV_ARCHIVE_SHA256_BY_TARGET
    assert preparer.UV_BINARY_SHA256_BY_TARGET == BUILDER.UV_BINARY_SHA256_BY_TARGET
    assert preparer.expected_uv_member("x86_64-unknown-linux-gnu") == (
        "uv-x86_64-unknown-linux-gnu/uv"
    )
    assert preparer.expected_uv_member("x86_64-pc-windows-msvc") == "uv.exe"


def test_sealed_basename_alone_does_not_select_packaged_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A familiar snapshot basename cannot forge the bootstrap-issued scope."""
    sealed_root = tmp_path / "python-runtime"
    app_root = sealed_root / "app"
    desktop_path = app_root / "ecosystem/defaultspack/defaultspack/desktop_app.py"
    desktop_path.parent.mkdir(parents=True)
    desktop_path.write_text(
        (ROOT / "tobkiri_runtime/ecosystem/defaultspack/defaultspack/desktop_app.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (sealed_root / "sealed-environment.v1.json").write_text("{}", encoding="utf-8")
    (sealed_root / "lease.v1").write_text("lease\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    old_path = sys.path[:]
    old_ecosystem = sys.modules.get("ecosystem")
    old_defaultspack = sys.modules.get("ecosystem.defaultspack")
    module = types.ModuleType("sealed_desktop_test")
    module.__file__ = str(desktop_path)
    module.__package__ = ""
    source = desktop_path.read_text(encoding="utf-8")
    try:
        exec(compile(source, str(desktop_path), "exec"), module.__dict__)
        sys.path = []
        for key in ("REPO", "RUMI_CORE_DIR", "RUMI_APP_DIR"):
            monkeypatch.setenv(key, str(external))
        assert module._sealed_app_root() is None
        module._ensure_import_path()
        assert module._sealed_app_root() is None
        assert str(external) in sys.path
    finally:
        sys.path = old_path
        if old_ecosystem is None:
            sys.modules.pop("ecosystem", None)
        else:
            sys.modules["ecosystem"] = old_ecosystem
        if old_defaultspack is None:
            sys.modules.pop("ecosystem.defaultspack", None)
        else:
            sys.modules["ecosystem.defaultspack"] = old_defaultspack


def test_explicit_scope_selects_custom_named_snapshot_for_defaultspack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual Defaultspack module accepts only the bootstrap scope."""
    sealed_root = tmp_path / "snapshot-7f3c"
    app_root = sealed_root / "app"
    desktop_path = app_root / "ecosystem/defaultspack/defaultspack/desktop_app.py"
    desktop_path.parent.mkdir(parents=True)
    desktop_path.write_text(
        (ROOT / "tobkiri_runtime/ecosystem/defaultspack/defaultspack/desktop_app.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (desktop_path.parent / "__init__.py").write_text("", encoding="utf-8")
    (desktop_path.parent / "native_webview.py").write_text(
        "SEALED_IMPORT_MARKER = True\n",
        encoding="utf-8",
    )
    domain_root = desktop_path.parents[1] / "domain"
    domain_root.mkdir()
    (domain_root / "__init__.py").write_text("", encoding="utf-8")
    (domain_root / "sealed_probe.py").write_text(
        "SEALED_IMPORT_MARKER = True\n",
        encoding="utf-8",
    )
    bundle_binding_module = app_root / "core_runtime" / "packaged_application_bundle.py"
    bundle_binding_module.parent.mkdir()
    (bundle_binding_module.parent / "__init__.py").write_text("", encoding="utf-8")
    bundle_binding_module.write_text(
        "def install_packvm_bundle_binding_from_sealed_scope(scope, module_file):\n"
        "    assert scope.packvm_bundle_binding_for(module_file) is None\n",
        encoding="utf-8",
    )
    manifest_path = sealed_root / "sealed-environment.v1.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    (sealed_root / "lease.v1").write_text("lease\n", encoding="utf-8")
    app_root.chmod(0o555)
    desktop_path.chmod(0o444)
    external = tmp_path / "external"
    external.mkdir()
    old_path = sys.path[:]
    old_ecosystem = sys.modules.get("ecosystem")
    old_defaultspack = sys.modules.get("ecosystem.defaultspack")
    old_bootstrap = sys.modules.get("tobkiri_sealed.bootstrap")
    old_package = sys.modules.get("tobkiri_sealed")
    old_top_level_defaultspack = sys.modules.get("defaultspack")
    old_native_webview = sys.modules.get("defaultspack.native_webview")
    old_domain = sys.modules.get("domain")
    old_domain_probe = sys.modules.get("domain.sealed_probe")
    old_core_runtime = sys.modules.get("core_runtime")
    old_bundle_binding = sys.modules.get("core_runtime.packaged_application_bundle")
    for module_name in (
        "defaultspack.native_webview",
        "defaultspack",
        "domain.sealed_probe",
        "domain",
        "core_runtime.packaged_application_bundle",
        "core_runtime",
    ):
        sys.modules.pop(module_name, None)
    module = types.ModuleType("custom_snapshot_desktop_test")
    module.__file__ = str(desktop_path)
    module.__package__ = ""
    core_runtime = types.ModuleType("core_runtime")
    core_runtime.__path__ = [str(app_root / "core_runtime")]  # type: ignore[attr-defined]
    bundle_binding = types.ModuleType("core_runtime.packaged_application_bundle")

    def install_packvm_bundle_binding_from_sealed_scope(scope: object, module_file: object) -> None:
        assert scope.packvm_bundle_binding_for(module_file) is None

    bundle_binding.install_packvm_bundle_binding_from_sealed_scope = (
        install_packvm_bundle_binding_from_sealed_scope
    )
    try:
        sys.path = old_path[:]
        sys.modules.pop("tobkiri_sealed.bootstrap", None)
        sys.modules.pop("tobkiri_sealed", None)
        sys.path.insert(0, str(ROOT / ".github/scripts/sealed_python_sources"))
        import tobkiri_sealed.bootstrap as bootstrap

        source = desktop_path.read_text(encoding="utf-8")
        exec(compile(source, str(desktop_path), "exec"), module.__dict__)
        sys.path = []
        sys.modules["core_runtime"] = core_runtime
        sys.modules["core_runtime.packaged_application_bundle"] = bundle_binding
        for key in ("REPO", "RUMI_CORE_DIR", "RUMI_APP_DIR"):
            monkeypatch.setenv(key, str(external))
        scope = bootstrap._SealedDispatchScope(
            bootstrap._SCOPE_CONSTRUCTOR_TOKEN,
            sealed_root,
            manifest_path,
            BUILDER._sha256_file(manifest_path),
            "a" * 64,
            bootstrap.ROLE_TARGETS["defaultspack"],
            None,
        )
        module.prepare_for_sealed_dispatch(scope)
        assert module._sealed_app_root() == app_root
        assert sys.path == [str(app_root), str(desktop_path.parents[1])]
        assert str(external) not in sys.path
        native_webview = importlib.import_module("defaultspack.native_webview")
        assert native_webview.SEALED_IMPORT_MARKER is True
        domain_probe = importlib.import_module("domain.sealed_probe")
        assert domain_probe.SEALED_IMPORT_MARKER is True
    finally:
        sys.path = old_path
        if old_ecosystem is None:
            sys.modules.pop("ecosystem", None)
        else:
            sys.modules["ecosystem"] = old_ecosystem
        if old_defaultspack is None:
            sys.modules.pop("ecosystem.defaultspack", None)
        else:
            sys.modules["ecosystem.defaultspack"] = old_defaultspack
        if old_bootstrap is None:
            sys.modules.pop("tobkiri_sealed.bootstrap", None)
        else:
            sys.modules["tobkiri_sealed.bootstrap"] = old_bootstrap
        if old_package is None:
            sys.modules.pop("tobkiri_sealed", None)
        else:
            sys.modules["tobkiri_sealed"] = old_package
        if old_top_level_defaultspack is None:
            sys.modules.pop("defaultspack", None)
        else:
            sys.modules["defaultspack"] = old_top_level_defaultspack
        if old_native_webview is None:
            sys.modules.pop("defaultspack.native_webview", None)
        else:
            sys.modules["defaultspack.native_webview"] = old_native_webview
        if old_domain is None:
            sys.modules.pop("domain", None)
        else:
            sys.modules["domain"] = old_domain
        if old_domain_probe is None:
            sys.modules.pop("domain.sealed_probe", None)
        else:
            sys.modules["domain.sealed_probe"] = old_domain_probe
        if old_core_runtime is None:
            sys.modules.pop("core_runtime", None)
        else:
            sys.modules["core_runtime"] = old_core_runtime
        if old_bundle_binding is None:
            sys.modules.pop("core_runtime.packaged_application_bundle", None)
        else:
            sys.modules["core_runtime.packaged_application_bundle"] = old_bundle_binding


def test_packvm_bundle_binding_is_scope_only_immutable_and_raw_bound(
    tmp_path: Path,
) -> None:
    """v3 accepts only exact immutable app resources and exposes no fallback."""

    output = _fixture_sources(tmp_path / "sealed", "x86_64-unknown-linux-gnu")[2]
    bundle, digests = _packvm_bundle_fixture(tmp_path / "bundle")
    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    old_path = sys.path[:]
    old_bootstrap = sys.modules.pop("tobkiri_sealed.bootstrap", None)
    old_package = sys.modules.pop("tobkiri_sealed", None)
    try:
        sys.path.insert(0, str(source_root))
        import tobkiri_sealed.bootstrap as bootstrap

        binding = bootstrap._verify_packvm_bundle_binding(
            str(bundle),
            digests["provisioning"],
            digests["helper"],
            "ABC1234567",
            digests["outer"],
        )
        assert binding is not None
        assert dict(binding) == {
            "root": str(bundle),
            "provisioning_sha256": "sha256:" + digests["provisioning"],
            "helper_manifest_sha256": "sha256:" + digests["helper"],
            "helper_team_id": "ABC1234567",
        }
        with pytest.raises(TypeError):
            binding["root"] = "forged"  # type: ignore[index]

        manifest = output / BUILDER.MANIFEST_FILENAME
        scope = bootstrap._SealedDispatchScope(
            bootstrap._SCOPE_CONSTRUCTOR_TOKEN,
            output,
            manifest,
            BUILDER._sha256_file(manifest),
            "a" * 64,
            bootstrap.ROLE_TARGETS["typed"],
            binding,
        )
        assert scope.packvm_bundle_binding_for(output / "app" / "app.py") == binding
        with pytest.raises(bootstrap.SealedBootstrapError, match="manifest-bound"):
            scope.packvm_bundle_binding_for(output / "app" / "kernel_entry.py")

        provisioning = bundle / "Contents/Resources/packvm-vz-provisioning.v1.json"
        _make_test_mutable(provisioning)
        provisioning.write_bytes(b"tampered\n")
        provisioning.chmod(0o444)
        with pytest.raises(bootstrap.SealedBootstrapError, match="bundle binding changed"):
            bootstrap._verify_packvm_bundle_binding(
                str(bundle),
                digests["provisioning"],
                digests["helper"],
                "ABC1234567",
                digests["outer"],
            )
        with pytest.raises(bootstrap.SealedBootstrapError, match="launch binding"):
            bootstrap._verify_packvm_bundle_binding(
                str(bundle),
                "",
                digests["helper"],
                "ABC1234567",
                digests["outer"],
            )
    finally:
        sys.path = old_path
        sys.modules.pop("tobkiri_sealed.bootstrap", None)
        sys.modules.pop("tobkiri_sealed", None)
        if old_bootstrap is not None:
            sys.modules["tobkiri_sealed.bootstrap"] = old_bootstrap
        if old_package is not None:
            sys.modules["tobkiri_sealed"] = old_package


def test_bootstrap_rejects_unknown_parent_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap-only options cannot be smuggled through the role boundary."""
    output = _fixture_sources(tmp_path, "x86_64-unknown-linux-gnu")[2]
    source_root = ROOT / ".github" / "scripts" / "sealed_python_sources"
    attestation_dir = tmp_path / "attestation"
    attestation_dir.mkdir()
    nonce = "b" * 64
    attestation = attestation_dir / f"startup-{nonce}.json"
    old_path = sys.path[:]
    old_prefix = sys.prefix
    old_base_prefix = sys.base_prefix
    old_executable = sys.executable
    sys.path = [str(output / "venv/lib/python3.13/site-packages")]
    sys.prefix = str(output / "venv")
    sys.base_prefix = str(output / "runtime")
    sys.executable = str(output / "venv/bin/python3")
    sys.modules.pop("tobkiri_sealed.bootstrap", None)
    sys.modules.pop("tobkiri_sealed", None)
    monkeypatch.setenv(
        "TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256",
        BUILDER._sha256_file(output / BUILDER.MANIFEST_FILENAME),
    )
    try:
        sys.path.insert(0, str(source_root))
        import tobkiri_sealed.bootstrap as bootstrap

        with pytest.raises(SystemExit):
            bootstrap.main(
                [
                    "--role",
                    "typed",
                    "--nonce",
                    nonce,
                    "--attestation",
                    str(attestation),
                    "--manifest",
                    str(output / BUILDER.MANIFEST_FILENAME),
                    "--environment-root",
                    str(output),
                    "--unknown-parent-option",
                    "--",
                ]
            )
    finally:
        sys.path = old_path
        sys.prefix = old_prefix
        sys.base_prefix = old_base_prefix
        sys.executable = old_executable
        sys.modules.pop("tobkiri_sealed.bootstrap", None)
        sys.modules.pop("tobkiri_sealed", None)


@pytest.mark.parametrize("case", ("tampered", "missing", "wrong-target", "extra"))
def test_validator_rejects_tamper_missing_wrong_target_and_extra(
    tmp_path: Path,
    case: str,
) -> None:
    """The validator fails closed for the core integrity failure classes."""
    output = _fixture_sources(tmp_path, "x86_64-unknown-linux-gnu")[2]
    if case == "tampered":
        _make_test_mutable(output / "app/kernel_entry.py")
        (output / "app/kernel_entry.py").write_bytes(b"tampered\n")
        target = "x86_64-unknown-linux-gnu"
    elif case == "missing":
        _make_test_mutable(output / "app")
        (output / "app/defaultspack_entry.py").unlink()
        target = "x86_64-unknown-linux-gnu"
    elif case == "wrong-target":
        target = "x86_64-pc-windows-msvc"
    else:
        _make_test_mutable(output)
        (output / "unlisted.bin").write_bytes(b"extra\n")
        target = "x86_64-unknown-linux-gnu"

    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER.validate_environment(output, target, run_native_smoke=False)


def test_validator_rejects_links_hardlinks_and_manifest_path_escape(tmp_path: Path) -> None:
    """A sealed tree cannot smuggle links or traversal through the inventory."""
    linked = _fixture_sources(tmp_path / "linked", "x86_64-unknown-linux-gnu")[2]
    _make_test_mutable(linked)
    (linked / "extra-link").symlink_to(linked / "lease.v1")
    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER.validate_environment(linked, "x86_64-unknown-linux-gnu", run_native_smoke=False)

    hardlinked = _fixture_sources(tmp_path / "hardlinked", "x86_64-unknown-linux-gnu")[2]
    _make_test_mutable(hardlinked)
    os.link(hardlinked / "lease.v1", hardlinked / "hardlink")
    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER.validate_environment(
            hardlinked,
            "x86_64-unknown-linux-gnu",
            run_native_smoke=False,
        )

    escaped = _fixture_sources(tmp_path / "escaped", "x86_64-unknown-linux-gnu")[2]
    manifest_path = escaped / BUILDER.MANIFEST_FILENAME
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["files"][0]["path"] = "../outside"
    _make_test_mutable(manifest_path)
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER.validate_environment(
            escaped,
            "x86_64-unknown-linux-gnu",
            run_native_smoke=False,
        )


def test_bootstrap_and_resource_wiring_match_the_fixed_contract() -> None:
    """Static checks cover the wire, raw digest, and resource boundary."""
    bootstrap = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    builder = BUILDER_PATH.read_text(encoding="utf-8")
    preparer = (ROOT / ".github" / "scripts" / "prepare_tauri_resources.py").read_text(
        encoding="utf-8"
    )
    build_rs = (ROOT / "tobkiri_launcher" / "src-tauri" / "build.rs").read_text(encoding="utf-8")
    rust_protocol = (
        ROOT / "tobkiri_launcher" / "src-tauri" / "src" / "sealed_python.rs"
    ).read_text(encoding="utf-8")
    protocol_path = ROOT / "tobkiri_launcher" / "src-tauri" / "src" / "sealed_python_protocol.rs"
    protocol_source = protocol_path.read_text(encoding="utf-8") if protocol_path.exists() else ""
    rust_contract = rust_protocol + protocol_source
    environment_schema = json.loads(
        (ROOT / ".github" / "schemas" / "sealed-python-environment.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    attestation_schema = json.loads(
        (ROOT / ".github" / "schemas" / "sealed-python-attestation.v2.schema.json").read_text(
            encoding="utf-8"
        )
    )
    tauri = json.loads(
        (ROOT / "tobkiri_launcher" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )

    assert "lease.v1" in bootstrap
    assert "LOCK_SH" in bootstrap and "LK_RLCK" in bootstrap
    assert 'values.index("--")' in bootstrap
    assert "io.tobkiri.sealed-python-launch.v3" in bootstrap
    assert BUILDER.ATTESTATION_SCHEMA == "io.tobkiri.sealed-python-attestation.v2"
    assert BUILDER.ATTESTATION_SCHEMA in bootstrap
    assert BUILDER.ATTESTATION_SCHEMA in rust_contract
    assert BUILDER.ATTESTATION_FILE_SCHEMA == "io.tobkiri.sealed-python-attestation-file.v1"
    assert BUILDER.ATTESTATION_FILE_SCHEMA in bootstrap
    assert BUILDER.ATTESTATION_FILE_SCHEMA in rust_contract
    assert "os.replace" in bootstrap
    assert "fsync" in bootstrap and "O_EXCL" in bootstrap
    assert "os.link" in bootstrap and "st_nlink" in bootstrap
    assert all(f'"{role}"' in bootstrap for role in ("typed", "defaultspack", "host_helper"))
    builder_strings = {
        node.value
        for node in ast.walk(ast.parse(builder))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not any(value.startswith("sha256:") for value in builder_strings)
    # The v3 parent wire supplies raw digests.  Bootstrap projects exactly the
    # two PackVM resource identities into the runtime's canonical prefixed
    # form after it has independently re-read and verified those resources.
    assert 'f"sha256:{provisioning_sha256}"' in bootstrap
    assert 'f"sha256:{helper_manifest_sha256}"' in bootstrap
    assert "sha256:" not in json.dumps(environment_schema)
    for field in (
        "schema",
        "nonce",
        "role",
        "environment_digest",
        "executable",
        "prefix",
        "base_prefix",
        "sys_path",
        "stdlib_sha256",
        "site_packages_sha256",
        "native_sha256",
        "runtime_overlay_sha256",
        "outer_runtime_manifest_sha256",
        "lifetime_lease",
    ):
        assert f'"{field}"' in bootstrap
    for marker in (
        '"-I"',
        '"-B"',
        '"tobkiri_sealed.bootstrap"',
        "_hashlib",
        "_ssl",
        "cryptography",
        '"typed"',
        '"defaultspack"',
        '"host_helper"',
        '"--application-bundle-root"',
        '"--packvm-provisioning-sha256"',
        '"--packvm-helper-manifest-sha256"',
        '"--packvm-helper-team-id"',
    ):
        assert marker in builder
    assert "--health" not in (
        ROOT / ".github" / "scripts" / "sealed_python_sources" / "app" / "defaultspack_entry.py"
    ).read_text(encoding="utf-8")
    assert "--headless" not in (
        ROOT / ".github" / "scripts" / "sealed_python_sources" / "app" / "host_helper_entry.py"
    ).read_text(encoding="utf-8")
    assert "python-runtime" in preparer
    assert "sealed-environment.v1.json" in preparer
    assert "python-runtime" in build_rs
    assert "bind_sealed_python_root" in build_rs
    assert "TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256" in build_rs
    assert environment_schema["$id"] == BUILDER.MANIFEST_SCHEMA
    assert environment_schema["$defs"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"
    assert attestation_schema["properties"]["nonce"]["pattern"] == "^[0-9a-f]{64}$"
    assert attestation_schema["properties"]["role"]["enum"] == [
        "typed",
        "defaultspack",
        "host_helper",
    ]
    for marker in (
        '"--nonce"',
        '"--attestation"',
        '"--manifest"',
        '"--environment-root"',
        '"--runtime-overlay-sha256"',
        '"--outer-runtime-manifest-sha256"',
        '"--application-bundle-root"',
        '"--packvm-provisioning-sha256"',
        '"--packvm-helper-manifest-sha256"',
        '"--packvm-helper-team-id"',
        '"--"',
        '"venv/bin/python3"',
        '"app/kernel_entry.py"',
        '"app/defaultspack_entry.py"',
        '"app/host_helper_entry.py"',
    ):
        assert marker in rust_contract
    if protocol_source:
        for marker in ('"typed"', '"defaultspack"', '"host_helper"'):
            assert marker in protocol_source
    assert tauri["bundle"]["resources"] == {"./gen/app": "app"}


def test_raw_manifest_digest_matches_compact_cross_language_contract() -> None:
    """The Python digest is raw SHA-256 over compact manifest JSON bytes."""
    records = [
        {
            "path": "a.txt",
            "size": 1,
            "sha256": "a" * 64,
            "executable": False,
        }
    ]
    compact = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()
    digest = BUILDER._files_digest(records)
    assert digest == hashlib.sha256(compact).hexdigest()
    assert not digest.startswith("sha256:")


def test_all_tauri_build_callsites_are_mac_release_gated() -> None:
    """No workflow or local production caller can publish Windows/Linux builds."""
    tracked = (
        subprocess.check_output(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
        )
        .decode("utf-8")
        .split("\0")
    )
    needle = "cargo tauri " + "build"
    all_hits = []
    callsites = []
    for relative in tracked:
        if not relative:
            continue
        try:
            text = (ROOT / relative).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if needle in text:
            all_hits.append(relative)
        if Path(relative).suffix not in {".sh", ".yml", ".yaml"}:
            continue
        if needle in text:
            callsites.append(relative)
    assert set(all_hits) == {
        ".github/workflows/desktop-installers.yml",
        ".github/workflows/release.yml",
        "scripts/build-and-sign.sh",
        "tobkiri_runtime/docs/ci_build_guide.md",
        "tobkiri_runtime/docs/quality_pack/claude_desktop_quality_pack.md",
        "tobkiri_runtime/tests/test_claude_quality_pack_contract.py",
        "tobkiri_runtime/tests/test_viewer_build_contract.py",
    }
    assert set(callsites) == {
        ".github/workflows/desktop-installers.yml",
        ".github/workflows/release.yml",
        "scripts/build-and-sign.sh",
    }

    desktop = (ROOT / ".github/workflows/desktop-installers.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    release_build = release.split("\n  gather:", 1)[0]
    assert "release workflow is currently macOS-only" in release
    for workflow in (desktop, release_build):
        assert "windows-latest" not in workflow
        assert "x86_64-pc-windows-msvc" not in workflow
        assert "x86_64-unknown-linux-gnu" not in workflow
        assert "if: runner.os != 'macOS'" not in workflow
        assert "--features" not in workflow
        for line in workflow.splitlines():
            if needle in line:
                assert "${{ matrix.target }}" in line

    helper = (ROOT / "scripts/build-and-sign.sh").read_text(encoding="utf-8")
    guard = 'if [[ "$mode" == "production" && "$presentation_platform" != "macos" ]]'
    assert guard in helper
    assert helper.index(guard) < helper.index(needle)
