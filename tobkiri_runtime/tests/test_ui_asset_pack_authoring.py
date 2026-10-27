"""UI delivery uses the real SDK compiler without inventing executable edges."""

from pathlib import Path
import json

import pytest

from core_runtime.pack_authoring import PackAuthoringError, build_python_pack
from core_runtime.ui_asset_pack_authoring import build_ui_asset_pack
from scripts.stage_compiled_ui_asset_pack_v4 import stage_compiled_ui_asset_pack
from tobkiri_host.artifact_compiler import compile_pack_root


def test_ui_assets_compile_without_functions_or_capabilities(tmp_path: Path) -> None:
    root = build_ui_asset_pack(
        tmp_path / "pack", pack_id="example.ui", version="1.0.0",
        display_name="Example UI", assets={
            "ui/shell.html": b"<html>UI</html>",
            "ui/shell-app.js": b"throw new Error('must not execute');",
        },
    )
    compiled = compile_pack_root(root)
    manifest = json.loads((root / "pack.v4.json").read_text())
    assert compiled.artifact.pack_id == "example.ui"
    assert manifest["pack"]["kind"] == "normal_sandbox"
    assert manifest["functions"] == manifest["contracts"] == []
    assert manifest["operation_catalog"] == manifest["provider_catalog"] == []
    assert manifest["requirements"]["capabilities"] == []
    assert manifest["requirements"]["pack_dependencies"] == {}
    assert manifest["requirements"]["secrets"] == []
    index = json.loads((root / "artifact-index.v4.json").read_text())
    ui_entries = [a for a in index["artifacts"] if a["path"].startswith("ui/")]
    assert len(ui_entries) == 2
    assert all(a["role"] == "asset" for a in ui_entries)
    assert json.loads((root / "executables.v4.json").read_text())["variants"] == []
    source = json.loads((root / "authoring-source.v1.json").read_text())
    assert source["source_api_version"] == "io.tobkiri.ui-asset-pack-source.v1"


@pytest.mark.parametrize("path", [
    "backend.py", "ui/backend.py", "ui/.env", "ui/../secret.js",
    "ui/.private/file.js", "/ui/shell.js", "ui\\shell.js",
])
def test_ui_asset_producer_rejects_nonpublic_paths(
    tmp_path: Path, path: str,
) -> None:
    with pytest.raises(PackAuthoringError):
        build_ui_asset_pack(
            tmp_path / "pack", pack_id="example.ui", version="1.0.0",
            display_name="Example UI", assets={"ui/shell.html": b"UI", path: b"X"},
        )
    assert not (tmp_path / "pack").exists()


def test_existing_python_authoring_still_rejects_empty_operations(
    tmp_path: Path,
) -> None:
    with pytest.raises(PackAuthoringError, match="cover all Contract"):
        build_python_pack(
            tmp_path / "pack", pack_id="example.ui", version="1.0.0",
            display_name="Example UI", contracts=[], functions=[],
            assets={"ui/shell.html": b"UI"},
        )


def test_ui_binary_assets_can_exceed_python_source_limit(tmp_path: Path) -> None:
    root = build_ui_asset_pack(
        tmp_path / "pack", pack_id="example.ui", version="1.0.0",
        display_name="Example UI", assets={
            "ui/shell.html": b"UI", "ui/model.task": b"x" * (1024 * 1024 + 1),
        },
    )
    assert compile_pack_root(root).artifact.pack_id == "example.ui"


@pytest.mark.parametrize("assets", [
    {}, {"ui/shell-app.js": b"JS"}, {"ui/shell.html": "UI"},
    {"ui/shell.html": b"UI", 42: b"X"},
])
def test_invalid_ui_inventory_is_rejected(tmp_path: Path, assets: dict) -> None:
    with pytest.raises(PackAuthoringError):
        build_ui_asset_pack(
            tmp_path / "pack", pack_id="example.ui", version="1.0.0",
            display_name="Example UI", assets=assets,
        )
    assert not (tmp_path / "pack").exists()


def test_ui_asset_target_is_never_replaced(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    (target / "kept.txt").write_text("keep")
    with pytest.raises(PackAuthoringError):
        build_ui_asset_pack(
            target, pack_id="example.ui", version="1.0.0",
            display_name="Example UI", assets={"ui/shell.html": b"UI"},
        )
    assert (target / "kept.txt").read_text() == "keep"


def test_staging_does_not_modify_the_source_or_bundled_catalog(
    tmp_path: Path,
) -> None:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "shell.html").write_bytes(b"UI")
    (ui / "shell-app.js").write_bytes(b"JS")
    (ui / "__init__.py").write_bytes(b"")
    (ui / ".shell-bundle-manifest.json").write_bytes(b"{}")
    catalog = Path(__file__).parents[1] / "schemas/pack_v4_catalog.v1.json"
    before = catalog.read_bytes()
    root = stage_compiled_ui_asset_pack(
        ui, tmp_path / "external", pack_id="example.ui", version="1.0.0",
    )
    assert compile_pack_root(root).artifact.pack_id == "example.ui"
    assert catalog.read_bytes() == before
    assert (ui / "shell-app.js").read_bytes() == b"JS"
    assert not (root / "ui/__init__.py").exists()


def test_staging_rejects_changed_bytes_between_inventory_and_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import stage_compiled_ui_asset_pack_v4 as producer

    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "shell.html").write_bytes(b"UI")
    original = producer.compiled_ui_artifacts

    def changed_inventory(path: Path) -> list[dict[str, str]]:
        result = original(path)
        (path / "shell.html").write_bytes(b"changed")
        return result

    monkeypatch.setattr(producer, "compiled_ui_artifacts", changed_inventory)
    with pytest.raises(ValueError, match="changed before capture"):
        stage_compiled_ui_asset_pack(
            ui, tmp_path / "external", pack_id="example.ui", version="1.0.0",
        )
    assert not (tmp_path / "external").exists()
