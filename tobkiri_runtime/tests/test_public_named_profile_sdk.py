"""Public Named Profile compilation and exact Pack content boundaries."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from core_runtime.profile_authoring import ProfileAuthoringError, build_named_profile
from core_runtime.profile_content_projection import (
    ProfileContentProjectionError,
    resolve_intent_projection,
    selected_projection_roots,
)
from core_runtime.profile_pack_projection import ProjectionPackSource
from tobkiri_protocol.validation import validate_document
from tests.test_pack_authoring import _build


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "ecosystem/defaultspack/v4"


def _intent(*, selected: bool = True, projection: bool = True) -> dict:
    intent = json.loads((TEMPLATE / "defaults.profile.intent.v1.json").read_bytes())
    intent.update(profile_id="example.named", display_name="Example named")
    intent["requested_edges"] = []
    intent["content_projections"] = []
    if selected:
        intent["packs"].append(
            {"pack_id": "example.echo", "artifact_digest": None, "role": "provider"}
        )
    if projection:
        intent["content_projections"] = [
            {
                "projection_id": "example.content",
                "kind": "profile_content",
                "artifact_root": "content",
                "source_pack_id": "example.echo",
                "source_artifact_digest": None,
                "content_digest": None,
            }
        ]
    return intent


def _pack(tmp_path: Path) -> Path:
    return _build(tmp_path / "pack", assets={"content/prompts/example.md": b"Example\n"})


def test_public_named_profile_pins_declared_selected_content(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    target = build_named_profile(
        tmp_path / "release",
        template_bundle=TEMPLATE,
        intent=_intent(),
        additional_pack_roots=[pack],
    )
    profile = validate_document((target / "authored.profile.v5.json").read_bytes(), "profile")
    lock = validate_document(
        (target / "authored.profile.lock.v5.json").read_bytes(), "profile_artifact_lock"
    )
    projection = profile["content_projections"][0]
    assert (
        projection["source_artifact_digest"]
        == json.loads((pack / "pack.v4.json").read_bytes())["pack"]["artifact_digest"]
    )
    assert lock["activation_authority"] == "unbound"
    assert lock["content_projections"] == profile["content_projections"]
    sources = {
        "example.echo": ProjectionPackSource(
            projection["source_artifact_digest"], target / "pack-resources/example.echo"
        )
    }
    selected = selected_projection_roots(profile["content_projections"], pack_sources=sources)
    assert selected[0][0] == "example.content"
    assert (selected[0][1] / "prompts/example.md").read_bytes() == b"Example\n"
    with pytest.raises(ProfileContentProjectionError, match="unselected"):
        selected_projection_roots(profile["content_projections"], pack_sources={})
    with pytest.raises(ProfileContentProjectionError):
        selected_projection_roots(profile["content_projections"])
    with pytest.raises(ProfileContentProjectionError, match="alias"):
        selected_projection_roots(
            [projection, {**projection, "projection_id": "example.alias"}], pack_sources=sources
        )
    (selected[0][1] / "prompts/example.md").write_bytes(b"Changed\n")
    with pytest.raises(ProfileContentProjectionError, match="differs"):
        selected_projection_roots(profile["content_projections"], pack_sources=sources)


def test_unselected_pack_projection_is_rejected_before_publication(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    with pytest.raises(ProfileContentProjectionError, match="unselected"):
        build_named_profile(
            tmp_path / "release",
            template_bundle=TEMPLATE,
            intent=_intent(selected=False),
            additional_pack_roots=[pack],
        )
    assert not (tmp_path / "release").exists()


def test_unselected_assets_are_absent_from_content_selection(tmp_path: Path) -> None:
    target = build_named_profile(
        tmp_path / "release",
        template_bundle=TEMPLATE,
        intent=_intent(projection=False),
        additional_pack_roots=[_pack(tmp_path)],
    )
    profile = json.loads((target / "authored.profile.v5.json").read_bytes())
    assert profile["content_projections"] == []


def test_unknown_selected_pack_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unavailable"):
        build_named_profile(
            tmp_path / "release", template_bundle=TEMPLATE, intent=_intent(projection=False)
        )
    assert not (tmp_path / "release").exists()


def test_duplicate_pack_and_existing_target_are_rejected(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    with pytest.raises(ProfileAuthoringError, match="duplicate"):
        build_named_profile(
            tmp_path / "release",
            template_bundle=TEMPLATE,
            intent=_intent(),
            additional_pack_roots=[pack, pack],
        )
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(ProfileAuthoringError, match="new directory"):
        build_named_profile(target, template_bundle=TEMPLATE, intent=_intent())


def test_template_mutation_and_symlink_are_rejected(tmp_path: Path) -> None:
    template = tmp_path / "template"
    shutil.copytree(TEMPLATE, template)
    path = next((template / "packs").glob("*.pack.v4.json"))
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="digest changed"):
        build_named_profile(tmp_path / "release", template_bundle=template, intent=_intent())
    link = tmp_path / "link"
    link.symlink_to(TEMPLATE, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        build_named_profile(tmp_path / "release", template_bundle=link, intent=_intent())


@pytest.mark.parametrize(
    "relative",
    ["../content", "/content", "content/../content", "content//prompts", "content\\prompts"],
)
def test_pack_content_traversal_is_rejected(tmp_path: Path, relative: str) -> None:
    pack = _pack(tmp_path)
    digest = json.loads((pack / "pack.v4.json").read_bytes())["pack"]["artifact_digest"]
    selection = {**_intent()["content_projections"][0], "artifact_root": relative}
    with pytest.raises(ProfileContentProjectionError, match="unsafe"):
        resolve_intent_projection(
            selection, pack_sources={"example.echo": ProjectionPackSource(digest, pack)}
        )
