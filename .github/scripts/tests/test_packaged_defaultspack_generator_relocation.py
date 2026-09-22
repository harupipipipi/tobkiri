"""Relocated-source tests for the official packaged Defaults generator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import py_compile
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _load_packaging_helpers() -> ModuleType:
    """Load the canonical isolated launcher helpers from the source tree."""
    helper_path = REPOSITORY_ROOT / "tobkiri_runtime/scripts/packaging_cleanup.py"
    spec = importlib.util.spec_from_file_location("relocation_packaging_cleanup", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"packaging helper is unavailable: {helper_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_PACKAGING_HELPERS = _load_packaging_helpers()
_SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_SOURCE_TREE = "89abcdef0123456789abcdef0123456789abcdef"


def _clean_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return the canonical isolated environment, including no Python hooks."""
    return _PACKAGING_HELPERS.isolated_packaging_environment(source)


def _source_manifest() -> dict[str, object]:
    """Load the one checked-in source closure manifest."""
    path = REPOSITORY_ROOT / "tobkiri_runtime/packaged_defaultspack_source_manifest.v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _copy_source_checkout(destination: Path) -> None:
    """Copy exactly the source closure required by the official generator."""
    manifest_path = (
        REPOSITORY_ROOT / "tobkiri_runtime/packaged_defaultspack_source_manifest.v1.json"
    )
    manifest_target = destination / "tobkiri_runtime/packaged_defaultspack_source_manifest.v1.json"
    manifest_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest_path, manifest_target)
    files = _source_manifest().get("files")
    if not isinstance(files, list):
        raise AssertionError("source closure manifest files are missing")
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise AssertionError("source closure manifest entry is malformed")
        relative = f"tobkiri_runtime/{entry['path']}"
        source = REPOSITORY_ROOT / relative
        if source.is_symlink() or not source.is_file():
            raise AssertionError(f"source closure file is unsafe: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    provenance_path = destination / "tobkiri_runtime/packaging-source-provenance.v1.json"
    provenance_path.write_bytes(
        json.dumps(
            {
                "schema": "io.tobkiri.packaging-source-provenance.v1",
                "source_commit": _SOURCE_COMMIT,
                "source_tree": _SOURCE_TREE,
                "source_clean": True,
                "source_manifest_sha256": hashlib.sha256(
                    manifest_target.read_bytes()
                ).hexdigest(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    provenance_path.chmod(0o400)


def _fixture(root: Path) -> tuple[Path, Path, Path]:
    """Create a small relocated source, bundle, and ELF artifact fixture."""
    checkout = root / "authoritative-source"
    _copy_source_checkout(checkout)
    bundle = root / "work/defaultspack/v4"
    shutil.copytree(
        checkout / "tobkiri_runtime/ecosystem/defaultspack/v4",
        bundle,
    )
    artifact = root / "release/Tobkiri.AppImage"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 10 + b">\x00fixture")
    artifact.chmod(0o755)
    return checkout, bundle, artifact


def test_relocated_source_checkout_preserves_all_locked_catalog_sidecars(
    tmp_path: Path,
) -> None:
    """The source snapshot includes every lock-bound catalog and its root catalog."""
    checkout, bundle, _ = _fixture(tmp_path / "catalog-closure")
    lock = json.loads((bundle / "bundle.lock.json").read_text(encoding="utf-8"))
    expected = {
        entry["path"]: entry["digest"]
        for entry in lock["entries"]
        if entry["kind"] == "executable_catalog"
    }
    assert len(expected) == 63
    source_root = checkout / "tobkiri_runtime"
    assert (source_root / "ecosystem/defaultspack/executables.v4.json").is_file()
    for relative, digest in expected.items():
        candidate = source_root / "ecosystem/defaultspack/v4" / relative
        assert candidate.is_file()
        assert f"sha256:{hashlib.sha256(candidate.read_bytes()).hexdigest()}" == digest


def _source_contract(checkout: Path) -> dict[str, str]:
    """Return the one formal provenance file bound to the relocated snapshot."""
    return {
        "source_provenance_file": os.fspath(
            checkout / "tobkiri_runtime/packaging-source-provenance.v1.json"
        ),
    }


def _generator_process(
    checkout: Path,
    bundle: Path,
    artifact: Path,
    *,
    environment: dict[str, str] | None = None,
    cwd: Path | None = None,
    source_contract: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the official generator from the relocated runtime package root."""
    source_root = checkout / "tobkiri_runtime"
    arguments = [
        "--source-artifact",
        os.fspath(artifact),
        "--bundle-root",
        os.fspath(bundle),
        "--artifact-root",
        os.fspath(bundle.parent / "platform-artifacts"),
        "--relative-path",
        "Tobkiri.AppImage",
        "--entrypoint",
        "Tobkiri.AppImage",
        "--platform",
        "linux",
        "--architecture",
        "x86_64",
        "--bundle-identity",
        "io.tobkiri.shell.tauri",
    ]
    if source_contract is not None:
        arguments.extend(
            [
                "--source-provenance-file",
                source_contract["source_provenance_file"],
            ]
        )
    return subprocess.run(
        _PACKAGING_HELPERS.isolated_python_module_command(
            sys.executable,
            "scripts.generate_packaged_defaultspack_v4_bundle",
            source_root,
            arguments,
        ),
        cwd=source_root if cwd is None else cwd,
        env=_clean_environment() if environment is None else _clean_environment(environment),
        capture_output=True,
        text=True,
        check=False,
    )


def _run_generator(checkout: Path, bundle: Path, artifact: Path) -> None:
    """Require a successful official generator run."""
    result = _generator_process(
        checkout,
        bundle,
        artifact,
        source_contract=_source_contract(checkout),
    )
    assert result.returncode == 0, result.stderr


def _output_bytes(root: Path) -> dict[str, bytes]:
    """Return regular output bytes in canonical relative-path order."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _run_help(checkout: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run module help and return its process result for negative tests."""
    return subprocess.run(
        _PACKAGING_HELPERS.isolated_python_module_command(
            sys.executable,
            "scripts.generate_packaged_defaultspack_v4_bundle",
            checkout / "tobkiri_runtime",
            ["--help"],
        ),
        cwd=checkout / "tobkiri_runtime",
        env=_clean_environment(environment),
        capture_output=True,
        text=True,
        check=False,
    )


def test_relocated_generator_is_deterministic_without_repository_imports(tmp_path: Path) -> None:
    """The official generator works and emits identical bytes after relocation."""
    first = _fixture(tmp_path / "first")
    second = _fixture(tmp_path / "second")
    _run_generator(*first)
    _run_generator(*second)

    assert _output_bytes(first[1]) == _output_bytes(second[1])
    assert _output_bytes(first[1].parent / "platform-artifacts") == _output_bytes(
        second[1].parent / "platform-artifacts"
    )
    assert not list(tmp_path.rglob(".tobkiri-defaultspack-transaction-*"))


def test_isolated_launcher_rejects_hostile_hooks_packages_and_cwd(
    tmp_path: Path,
) -> None:
    """Only the canonical root can affect a generator module launch."""
    checkout, bundle, artifact = _fixture(tmp_path / "hostile")
    hostile = tmp_path / "hostile-input"
    hostile.mkdir()
    marker = hostile / "executed.marker"
    marker_literal = repr(os.fspath(marker))
    (hostile / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({marker_literal}).write_text('sitecustomize')\n",
        encoding="utf-8",
    )
    (hostile / "usercustomize.py").write_text(
        f"from pathlib import Path; Path({marker_literal}).write_text('usercustomize')\n",
        encoding="utf-8",
    )
    (hostile / "startup.py").write_text(
        f"from pathlib import Path; Path({marker_literal}).write_text('startup')\n",
        encoding="utf-8",
    )
    fake_scripts = hostile / "scripts"
    fake_scripts.mkdir()
    (fake_scripts / "__init__.py").write_text("\n", encoding="utf-8")
    (fake_scripts / "generate_packaged_defaultspack_v4_bundle.py").write_text(
        f"from pathlib import Path; Path({marker_literal}).write_text('fake-module')\n",
        encoding="utf-8",
    )
    fake_protocol = hostile / "tobkiri_protocol"
    fake_protocol.mkdir()
    (fake_protocol / "__init__.py").write_text(
        f"from pathlib import Path; Path({marker_literal}).write_text('fake-package')\n",
        encoding="utf-8",
    )
    poisoned = dict(os.environ)
    poisoned.update(
        {
            "PYTHONPATH": os.fspath(hostile),
            "PYTHONHOME": os.fspath(hostile / "not-python"),
            "PYTHONSTARTUP": os.fspath(hostile / "startup.py"),
            "REPO": os.fspath(hostile),
            "RUMI_CORE_DIR": os.fspath(hostile),
            "LD_LIBRARY_PATH": os.fspath(hostile),
            "DYLD_INSERT_LIBRARIES": os.fspath(hostile / "fake.dylib"),
        }
    )
    unsafe_environment = dict(os.environ)
    unsafe_environment["PYTHONPATH"] = os.fspath(hostile)
    unsafe = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "scripts.generate_packaged_defaultspack_v4_bundle",
            "--help",
        ],
        cwd=hostile,
        env=unsafe_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unsafe.returncode == 0
    assert marker.exists(), "control launch should demonstrate the hostile import"
    marker.unlink()

    safe = _generator_process(
        checkout,
        bundle,
        artifact,
        environment=poisoned,
        cwd=hostile,
        source_contract=_source_contract(checkout),
    )
    assert safe.returncode == 0, safe.stderr
    assert not marker.exists(), "isolated launch executed hostile Python input"


def test_relocated_generator_never_spawns_path_git(
    tmp_path: Path,
) -> None:
    """The preverified generator never spawns a Git executable from PATH."""
    checkout, bundle, artifact = _fixture(tmp_path / "no-git")
    fake_path = tmp_path / "fake-path"
    fake_path.mkdir()
    marker = tmp_path / "path-git-executed"
    fake_git = fake_path / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' path-git > {shlex.quote(os.fspath(marker))}\n"
        "exit 97\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    environment = {"PATH": os.fspath(fake_path)}
    assert "PATH" not in _clean_environment(environment)
    result = _generator_process(
        checkout,
        bundle,
        artifact,
        environment=environment,
        source_contract=_source_contract(checkout),
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "generator spawned Git selected through PATH"


def test_relocated_generator_rejects_malformed_source_provenance(
    tmp_path: Path,
) -> None:
    """Formal source provenance must be a complete lowercase identity."""
    checkout, bundle, artifact = _fixture(tmp_path / "bad-provenance")
    contract = _source_contract(checkout)
    provenance_path = Path(contract["source_provenance_file"])
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["source_tree"] = "not-a-source-tree"
    provenance_path.chmod(0o600)
    provenance_path.write_text(
        json.dumps(provenance, separators=(",", ":")), encoding="utf-8"
    )
    provenance_path.chmod(0o400)
    result = _generator_process(
        checkout,
        bundle,
        artifact,
        source_contract=contract,
    )
    assert result.returncode != 0
    stderr = result.stderr.strip()
    safe_error = stderr.splitlines()[-1]
    assert "SourceProvenanceError:" in safe_error
    assert "[provenance.source_tree]" in safe_error
    assert "source_tree must be a full lowercase 40-hex identity" in safe_error
    assert "not-a-source-tree" not in safe_error
    assert os.fspath(tmp_path) not in safe_error
    assert os.fspath(checkout) not in safe_error
    assert "ValueError:" not in safe_error
    assert "During handling of the above exception" not in stderr


def test_relocated_generator_rejects_missing_tampered_or_external_cleanup(tmp_path: Path) -> None:
    """A missing or changed sibling cannot be replaced by an external helper."""
    missing_checkout, _, _ = _fixture(tmp_path / "missing")
    missing_helper = missing_checkout / "tobkiri_runtime/scripts/packaging_cleanup.py"
    missing_helper.unlink()
    external = tmp_path / "external"
    external.mkdir()
    (external / "packaging_cleanup.py").write_text("def remove_owned_path(*args, **kwargs): pass\n")
    environment = _clean_environment()
    environment["PYTHONPATH"] = os.fspath(external)
    missing = _run_help(missing_checkout, environment)
    assert missing.returncode != 0
    assert "source closure" in missing.stderr or "packaging_cleanup" in missing.stderr

    tampered_checkout, _, _ = _fixture(tmp_path / "tampered")
    tampered_helper = tampered_checkout / "tobkiri_runtime/scripts/packaging_cleanup.py"
    tampered_helper.write_text("this is not valid Python\n")
    tampered = _run_help(tampered_checkout, _clean_environment())
    assert tampered.returncode != 0
    assert "source closure" in tampered.stderr or "SyntaxError" in tampered.stderr


def test_relocated_generator_rejects_manifest_missing_tamper_extra_and_symlink(
    tmp_path: Path,
) -> None:
    """The shared source manifest fails closed for every closure mutation."""
    cases = (
        "missing",
        "tampered",
        "extra",
        "symlink",
        "pyc",
        "pyo",
        "cache",
        "empty-cache",
        "valid-hash-pyc",
    )
    for case in cases:
        checkout, _, _ = _fixture(tmp_path / case)
        manifest = _source_manifest()
        entries = manifest["files"]
        assert isinstance(entries, list) and entries
        relative = entries[0]["path"]
        target = checkout / "tobkiri_runtime" / relative
        if case == "missing":
            target.unlink()
        elif case == "tampered":
            target.write_bytes(b"tampered source closure")
        elif case == "extra":
            extra = checkout / "tobkiri_runtime/scripts/extra-source.py"
            extra.write_text("extra = True\n", encoding="utf-8")
        elif case in {"pyc", "pyo"}:
            extra = checkout / f"tobkiri_runtime/scripts/extra-source.{case}"
            extra.write_bytes(b"\x00pyc-extra-attack\x00")
        elif case == "cache":
            extra = checkout / "tobkiri_runtime/scripts/__pycache__/extra.pyc"
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_bytes(b"\x00pyc-cache-attack\x00")
        elif case == "empty-cache":
            (checkout / "tobkiri_runtime/scripts/__pycache__").mkdir()
        elif case == "valid-hash-pyc":
            source = tmp_path / "valid_hash_extra.py"
            source.write_text("extra = 'valid hash pyc'\n", encoding="utf-8")
            extra = checkout / "tobkiri_runtime/scripts/__pycache__/extra.pyc"
            extra.parent.mkdir(parents=True, exist_ok=True)
            py_compile.compile(
                os.fspath(source),
                cfile=os.fspath(extra),
                doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
            )
        else:
            outside = tmp_path / "outside.py"
            outside.write_text("outside = True\n", encoding="utf-8")
            target.unlink()
            target.symlink_to(outside)
        result = _run_help(checkout, _clean_environment())
        assert result.returncode != 0, case
        assert "source closure" in result.stderr.lower() or "symlink" in result.stderr.lower()


def test_relocated_generator_rejects_missing_authoritative_input(tmp_path: Path) -> None:
    """A missing canonical source input cannot be silently regenerated."""
    checkout, bundle, artifact = _fixture(tmp_path / "missing-input")
    (bundle / "bundle.lock.json").unlink()
    result = _generator_process(
        checkout,
        bundle,
        artifact,
        source_contract=_source_contract(checkout),
    )
    assert result.returncode != 0
    assert "bundle.lock.json" in result.stderr
