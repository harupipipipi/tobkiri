from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

import pytest

from scripts.profile_compatibility_provenance import validate_compatibility_profile
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_document


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"
GENERATOR = ROOT / "scripts" / "generate_defaultspack_v4_bundle.py"
SOURCE_COMMIT = "f297890d29194ed5fb256a2d8351f00472c3d46d"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("defaultspack_core_generator", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _provenance_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _assert_complete_profile_release(bundle: Path) -> None:
    """Assert the immutable Profile closure that canonical publication owns."""

    profile_path = bundle / "defaults.profile.v4.json"
    lock_path = bundle / "defaults.profile.lock.v5.json"
    provenance_path = bundle / "defaults.release.provenance.json"
    profile_raw = profile_path.read_bytes()
    lock_raw = lock_path.read_bytes()
    profile = validate_document(profile_raw, "profile")
    lock = validate_document(lock_raw, "profile_artifact_lock")
    provenance = validate_document(
        provenance_path.read_bytes(),
        "profile_release_provenance",
    )
    validate_compatibility_profile(profile)

    bundle_raw = (bundle / "bundle.lock.json").read_bytes()
    bundle_lock = json.loads(bundle_raw)
    profile_entry = next(
        item for item in bundle_lock["entries"] if item["path"] == "defaults.profile.v4.json"
    )
    assert profile_entry["digest"] == _sha256(profile_raw)
    assert lock["profile_revision"] == canonical_digest(profile)
    assert lock["bundle_digest"] == _sha256(bundle_raw)
    assert provenance["release_digest"] == canonical_digest(
        {key: value for key, value in provenance.items() if key != "release_digest"}
    )
    assert provenance["profile_revision"] == lock["profile_revision"]
    assert provenance["catalog"]["bundle_digest"] == lock["bundle_digest"]
    assert {item["path"]: item["digest"] for item in provenance["outputs"]} == {
        _provenance_path(profile_path): _sha256(profile_raw),
        _provenance_path(lock_path): _sha256(lock_raw),
    }
    for item in provenance["source_inputs"]:
        source = Path(item["path"])
        if not source.is_absolute():
            source = ROOT / source
        assert source.is_file()
        assert _sha256(source.read_bytes()) == item["digest"]


def test_core_generator_transaction_rolls_back_every_output(
    tmp_path: Path,
) -> None:
    generator = _load_generator()
    rendered = generator._render(SOURCE_COMMIT)
    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE, copied)
    staged_render = {copied / path.relative_to(BUNDLE): raw for path, raw in rendered.items()}
    before = _snapshot(copied)
    generator.BUNDLE = copied

    def fail(stage: str) -> None:
        if stage == "after_backup":
            raise RuntimeError("injected publication failure")

    with pytest.raises(RuntimeError, match="injected publication failure"):
        generator._publish(staged_render, fault=fail)

    assert _snapshot(copied) == before


def test_checked_in_bundle_matches_canonical_render() -> None:
    generator = _load_generator()
    rendered = generator._render()
    rendered.update(
        generator._render_profile_release(
            generator.BUNDLE,
            source_bundle_root=generator.BUNDLE,
        )
    )
    expected = {
        path.relative_to(generator.BUNDLE).as_posix(): raw for path, raw in rendered.items()
    }
    expected["defaults.profile.intent.v1.json"] = (
        generator.BUNDLE / "defaults.profile.intent.v1.json"
    ).read_bytes()

    assert _snapshot(generator.BUNDLE) == expected


def test_core_generator_publishes_candidate_before_profile_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new Profile Pack is only resolvable from the publication stage."""

    generator = _load_generator()
    rendered = {generator.BUNDLE / "bundle.lock.json": b"candidate\n"}
    published: list[dict[Path, bytes]] = []

    monkeypatch.setattr(sys, "argv", [str(GENERATOR)])
    monkeypatch.setattr(generator, "_render", lambda source_commit: rendered)
    monkeypatch.setattr(
        generator,
        "_render_profile_release",
        lambda *args, **kwargs: pytest.fail("normal publication resolved the old bundle"),
    )
    monkeypatch.setattr(generator, "_publish", lambda candidate: published.append(candidate))

    assert generator.main() == 0
    assert published == [rendered]


def test_canonical_pack_projections_are_generator_owned_derivatives() -> None:
    """Each canonical Pack input has one source-bound bundle derivative."""

    generator = _load_generator()
    rendered = generator._render(SOURCE_COMMIT)
    projections = [
        (output, source)
        for output, source in generator._canonical_pack_sources().items()
        if output != source
    ]

    assert len(projections) == 65
    assert any(
        source.parent.name == "rumi_command_protocol_pack"
        for _, source in projections
    )
    for output, source in projections:
        canonical_raw = source.read_bytes()
        canonical = validate_document(canonical_raw, "pack")
        derivative_raw = rendered[output]
        derivative = validate_document(derivative_raw, "pack")
        provenance = derivative["provenance"]

        assert derivative_raw != canonical_raw
        assert provenance["schema"] == "io.tobkiri.provenance.v2"
        assert provenance["source_kind"] == "generated"
        assert provenance["source_path"] == source.relative_to(ROOT).as_posix()
        assert provenance["source_digest"] == _sha256(canonical_raw)
        assert provenance["repository_commit"] == canonical["provenance"]["repository_commit"]
        assert provenance["generator"] == ("tobkiri.scripts.generate_defaultspack_v4_bundle")
        assert (
            derivative["integrity"]["source_identity"] != canonical["integrity"]["source_identity"]
        )

        canonical_catalog = generator._executable_catalog_source(source, canonical)
        assert canonical_catalog is not None
        output_catalog = output.with_name(f"{derivative['pack']['id']}.executables.v4.json")
        derivative_catalog_raw = rendered[output_catalog]
        derivative_catalog = validate_document(
            derivative_catalog_raw,
            "executable_catalog",
        )
        assert derivative_catalog_raw != canonical_catalog.read_bytes()
        assert derivative_catalog["source_identity"] == derivative["integrity"]["source_identity"]
        canonical_catalog_document = validate_document(
            canonical_catalog.read_bytes(),
            "executable_catalog",
        )
        assert (
            derivative_catalog["materialization_catalog_digest"]
            == (canonical_catalog_document["catalog_digest"])
        )
        assert derivative_catalog["variants"] == canonical_catalog_document["variants"]


def test_core_generator_publishes_complete_profile_release_closure(
    tmp_path: Path,
) -> None:
    """Refresh Profile, lock, and release provenance together on publication."""

    generator = _load_generator()
    rendered = generator._render(SOURCE_COMMIT)
    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE, copied)
    staged_render = {copied / path.relative_to(BUNDLE): raw for path, raw in rendered.items()}
    generator.BUNDLE = copied

    generator._publish(staged_render)

    _assert_complete_profile_release(copied)


def test_core_generator_transaction_rejects_destination_symlink(
    tmp_path: Path,
) -> None:
    generator = _load_generator()
    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE, copied)
    outside = tmp_path / "outside.json"
    target = copied / "packs" / "defaults-basepack.pack.v4.json"
    target.unlink()
    target.symlink_to(outside)
    generator.BUNDLE = copied

    with pytest.raises(ValueError, match="contains a symlink"):
        generator._publish(
            {
                target: b"{}\n",
                copied / "bundle.lock.json": (copied / "bundle.lock.json").read_bytes(),
            }
        )

    assert not outside.exists()


def test_core_generator_check_stage_rejects_destination_symlink(
    tmp_path: Path,
) -> None:
    """Check-only staging must not follow a bundle child symlink."""

    generator = _load_generator()
    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE, copied)
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    target = copied / "packs" / "defaults-basepack.pack.v4.json"
    target.unlink()
    target.symlink_to(outside)
    generator.BUNDLE = copied

    with pytest.raises(ValueError, match="rendered path contains a symlink"):
        generator._render_staged_profile_release(
            {
                target: b"candidate\n",
                copied / "bundle.lock.json": (copied / "bundle.lock.json").read_bytes(),
            }
        )

    assert outside.read_text(encoding="utf-8") == "outside"


def test_core_generator_script_entrypoint_is_independent_of_cwd() -> None:
    """The documented file entrypoint imports the runtime from a fresh checkout."""
    result = subprocess.run(
        [sys.executable, "-B", str(GENERATOR), "--check"],
        cwd=ROOT.parent,
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
