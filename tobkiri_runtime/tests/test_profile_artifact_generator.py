"""Contract tests for author intent and generated Named Profile artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from scripts import generate_defaultspack_v4_bundle as canonical_generator
from scripts import generate_profile_artifacts as generator
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.provenance import repository_tree_digest
from tobkiri_protocol.validation import validate_document


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _copy_bundle(tmp_path: Path) -> Path:
    target = tmp_path / "v4"
    shutil.copytree(BUNDLE, target)
    return target


def _paths(bundle: Path) -> dict[str, Path]:
    return {
        "intent": bundle / "defaults.profile.intent.v1.json",
        "compatibility": bundle / "defaults.profile.v4.json",
        "lock": bundle / "defaults.profile.lock.v5.json",
        "provenance": bundle / "defaults.release.provenance.json",
    }


def _render(bundle: Path) -> dict[Path, bytes]:
    paths = _paths(bundle)
    return generator.render(
        bundle_root=bundle,
        intent_path=paths["intent"],
        compatibility_path=paths["compatibility"],
        lock_path=paths["lock"],
        provenance_path=paths["provenance"],
    )


def _check(bundle: Path) -> int:
    paths = _paths(bundle)
    return generator.main(
        [
            "--bundle-root",
            str(bundle),
            "--intent",
            str(paths["intent"]),
            "--compatibility-profile",
            str(paths["compatibility"]),
            "--lock",
            str(paths["lock"]),
            "--provenance",
            str(paths["provenance"]),
            "--check",
        ]
    )


def _publish(rendered: dict[Path, bytes]) -> None:
    for path, raw in rendered.items():
        path.write_bytes(raw)


def _release_bytes(bundle: Path) -> dict[str, bytes]:
    paths = _paths(bundle)
    paths["bundle_lock"] = bundle / "bundle.lock.json"
    return {name: path.read_bytes() for name, path in paths.items() if name != "intent"}


def test_checked_in_profile_artifacts_are_deterministic_and_schema_valid() -> None:
    rendered = _render(BUNDLE)
    assert all(path.read_bytes() == raw for path, raw in rendered.items())

    intent = validate_document(
        (BUNDLE / "defaults.profile.intent.v1.json").read_bytes(),
        "profile_intent",
    )
    compatibility = validate_document((BUNDLE / "defaults.profile.v4.json").read_bytes(), "profile")
    lock = validate_document(
        (BUNDLE / "defaults.profile.lock.v5.json").read_bytes(),
        "profile_artifact_lock",
    )
    provenance = validate_document(
        (BUNDLE / "defaults.release.provenance.json").read_bytes(),
        "profile_release_provenance",
    )

    assert "provenance" not in intent
    assert intent["intent_api_version"] == "io.tobkiri.profile-intent.v1"
    assert compatibility["provenance"]["source_path"].endswith("defaults.profile.v4.json")
    assert compatibility["provenance"]["schema"] == "io.tobkiri.provenance.v1"
    assert compatibility["provenance"]["normative"] is False
    assert compatibility["provenance"]["repository_commit"] == "working-tree"
    assert compatibility["provenance"]["repository_tree"] == repository_tree_digest(
        ROOT,
        [Path(generator.__file__), *generator.COMPATIBILITY_PROVENANCE_INPUTS],
    )
    evidence_paths = [item["path"] for item in compatibility["provenance"]["evidence"]]
    assert evidence_paths == sorted(evidence_paths)
    assert "scripts/profile_compatibility_provenance.py" in evidence_paths
    assert lock["profile_revision"] == canonical_digest(compatibility)
    assert lock["activation_authority"] == "unbound"
    assert lock["profile_definition_digest"] == canonical_digest(intent)
    assert lock["closure_digest"] == canonical_digest(
        {
            "effective_set": lock["effective_set"],
            "content_projections": lock["content_projections"],
        }
    )
    assert lock["lock_digest"] == canonical_digest(
        {key: value for key, value in lock.items() if key != "lock_digest"}
    )
    assert len(lock["effective_set"]) > len(intent["packs"])
    assert lock["variant_pins"]
    assert provenance["profile_revision"] == lock["profile_revision"]
    source_paths = {item["path"] for item in provenance["source_inputs"]}
    assert any(path.endswith("defaults.profile.intent.v1.json") for path in source_paths)
    assert provenance["generator"]["path"].endswith("generate_profile_artifacts.py")
    assert not any(path.endswith("generate_defaultspack_v4_bundle.py") for path in source_paths)
    assert provenance["release_digest"] == canonical_digest(
        {key: value for key, value in provenance.items() if key != "release_digest"}
    )


@pytest.mark.parametrize(
    ("state", "repository_commit"),
    [
        ("needs_resolution", "a" * 40),
        ("resolved", "working-tree"),
    ],
)
def test_compatibility_profile_rejects_normative_unresolved_provenance(
    state: str, repository_commit: str
) -> None:
    profile = json.loads((BUNDLE / "defaults.profile.v4.json").read_text())
    profile["state"] = state
    profile["provenance"]["normative"] = True
    profile["provenance"]["repository_commit"] = repository_commit

    with pytest.raises(ValueError, match="normative provenance"):
        generator.validate_compatibility_profile(profile)


def test_canonical_bundle_render_preserves_non_authoritative_profile() -> None:
    """The compatibility projection cannot regain authority during canonicalization."""

    profile_path = BUNDLE / "defaults.profile.v4.json"
    rendered = canonical_generator._render()
    profile = json.loads(rendered[profile_path])

    assert profile["provenance"]["normative"] is False
    generator.validate_compatibility_profile(profile)


def test_roundtrip_preserves_bundle_compatibility_and_output_bytes(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    first = _render(bundle)
    _publish(first)
    second = _render(bundle)
    assert first == second
    assert BundledCatalog.load(bundle).profiles.keys() == {"defaults"}

    bundle_lock = json.loads((bundle / "bundle.lock.json").read_text())
    compatibility = bundle / "defaults.profile.v4.json"
    entry = next(item for item in bundle_lock["entries"] if item["path"] == compatibility.name)
    assert entry["digest"] == _sha256(compatibility.read_bytes())


@pytest.mark.parametrize(
    "artifact_name",
    [
        "defaults.profile.v4.json",
        "defaults.profile.lock.v5.json",
        "defaults.release.provenance.json",
    ],
)
def test_check_fails_closed_on_generated_artifact_tamper(
    tmp_path: Path, artifact_name: str
) -> None:
    bundle = _copy_bundle(tmp_path)
    _publish(_render(bundle))
    artifact = bundle / artifact_name
    artifact.write_bytes(artifact.read_bytes() + b" ")
    assert _check(bundle) == 1


def test_check_fails_closed_on_intent_drift_and_catalog_tamper(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    _publish(_render(bundle))
    intent_path = bundle / "defaults.profile.intent.v1.json"
    intent = json.loads(intent_path.read_text())
    intent["display_name"] = "Changed Named Profile"
    intent_path.write_text(json.dumps(intent, indent=2) + "\n")
    assert _check(bundle) == 1
    expected = _render(bundle)
    assert (
        expected[bundle / "defaults.profile.v4.json"]
        != (bundle / "defaults.profile.v4.json").read_bytes()
    )

    pack_path = bundle / "packs" / "defaultspack.pack.v4.json"
    pack_path.write_bytes(pack_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="bundle input digest changed"):
        _render(bundle)


def test_generator_applies_to_non_defaults_named_profile(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    defaults_intent = bundle / "defaults.profile.intent.v1.json"
    named_intent = bundle / "research.profile.intent.v1.json"
    intent = json.loads(defaults_intent.read_text())
    intent["profile_id"] = "research"
    intent["display_name"] = "Research"
    named_intent.write_text(json.dumps(intent, indent=2) + "\n")

    named_compatibility = bundle / "research.profile.v4.json"
    named_lock = bundle / "research.profile.lock.v5.json"
    named_provenance = bundle / "research.release.provenance.json"
    bundle_lock_path = bundle / "bundle.lock.json"
    bundle_lock = json.loads(bundle_lock_path.read_text())
    for entry in bundle_lock["entries"]:
        if entry["path"] == "defaults.profile.v4.json":
            entry["path"] = named_compatibility.name
    bundle_lock_path.write_text(json.dumps(bundle_lock, indent=2) + "\n")
    (bundle / "defaults.profile.v4.json").rename(named_compatibility)

    rendered = generator.render(
        bundle_root=bundle,
        intent_path=named_intent,
        compatibility_path=named_compatibility,
        lock_path=named_lock,
        provenance_path=named_provenance,
    )
    profile = json.loads(rendered[named_compatibility])
    lock = json.loads(rendered[named_lock])
    assert profile["profile_id"] == "research"
    assert lock["profile_id"] == "research"
    assert lock["effective_set"]


def test_render_rejects_symlinked_intent_without_publication(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    before = _release_bytes(bundle)
    intent = bundle / "defaults.profile.intent.v1.json"
    external = tmp_path / "external-intent.json"
    external.write_bytes(intent.read_bytes())
    intent.unlink()
    intent.symlink_to(external)

    with pytest.raises(ValueError, match="symlink component"):
        _render(bundle)
    assert _release_bytes(bundle) == before


def test_render_rejects_output_symlink_and_parent_escape(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    before = _release_bytes(bundle)
    lock_path = bundle / "defaults.profile.lock.v5.json"
    external = tmp_path / "external-lock.json"
    external.write_bytes(lock_path.read_bytes())
    lock_path.unlink()
    lock_path.symlink_to(external)

    with pytest.raises(ValueError, match="symlink component"):
        _render(bundle)
    assert external.read_bytes() == before["lock"]
    assert _release_bytes(bundle) == before

    lock_path.unlink()
    lock_path.write_bytes(before["lock"])
    paths = _paths(bundle)
    with pytest.raises(ValueError, match="escapes bundle root"):
        generator.render(
            bundle_root=bundle,
            intent_path=paths["intent"],
            compatibility_path=tmp_path / "escaped.profile.v4.json",
            lock_path=paths["lock"],
            provenance_path=paths["provenance"],
        )
    assert _release_bytes(bundle) == before


def test_render_rejects_symlinked_locked_bundle_input(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    before = _release_bytes(bundle)
    pack = bundle / "packs" / "defaultspack.pack.v4.json"
    external = tmp_path / "external-pack.json"
    external.write_bytes(pack.read_bytes())
    pack.unlink()
    pack.symlink_to(external)

    with pytest.raises(ValueError, match="symlink component"):
        _render(bundle)
    assert _release_bytes(bundle) == before


@pytest.mark.parametrize("phase", ["before_exchange", "after_exchange"])
def test_atomic_publication_rolls_back_complete_release_on_failure(
    tmp_path: Path, phase: str
) -> None:
    bundle = _copy_bundle(tmp_path)
    before = _release_bytes(bundle)
    intent_path = bundle / "defaults.profile.intent.v1.json"
    intent = json.loads(intent_path.read_text())
    intent["display_name"] = "Changed release"
    intent_path.write_text(json.dumps(intent, indent=2) + "\n")
    rendered = _render(bundle)

    def fail(requested_phase: str, _stage: Path) -> None:
        if requested_phase == phase:
            raise RuntimeError("injected publication failure")

    with pytest.raises(RuntimeError, match="injected publication failure"):
        generator._publish(rendered, bundle, fault=fail)
    assert _release_bytes(bundle) == before
    assert BundledCatalog.load(bundle).profiles.keys() == {"defaults"}


@pytest.mark.parametrize("mutation", ["symlink", "catalog_bytes"])
def test_staged_tree_race_is_rejected_before_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    bundle = _copy_bundle(tmp_path)
    before = _release_bytes(bundle)
    rendered = _render(bundle)
    exchanged = False

    def exchange(_left: Path, _right: Path) -> None:
        nonlocal exchanged
        exchanged = True
        raise AssertionError("invalid stage reached directory exchange")

    def mutate(phase: str, stage: Path) -> None:
        if phase != "before_validation":
            return
        target = stage / "packs" / "defaultspack.pack.v4.json"
        if mutation == "symlink":
            external = tmp_path / "staged-external-pack.json"
            external.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(external)
        else:
            target.write_bytes(target.read_bytes() + b" ")

    monkeypatch.setattr(generator, "_exchange_directories", exchange)
    with pytest.raises(ValueError, match="staged bundle|bundle input digest changed"):
        generator._publish(rendered, bundle, fault=mutate)
    assert exchanged is False
    assert _release_bytes(bundle) == before
