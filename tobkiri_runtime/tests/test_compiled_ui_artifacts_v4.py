"""Compiled UI declaration cannot import Source or alter other Pack authority."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from scripts import compiled_ui_artifacts_v4 as module
from scripts.compiled_ui_artifacts_v4 import (
    catalog_with_compiled_ui,
    compiled_ui_artifacts,
)


def _ui(tmp_path: Path) -> Path:
    root = tmp_path / "ui"
    root.mkdir()
    (root / "shell.html").write_text("<html>signed UI</html>")
    (root / "shell-app.js").write_text("console.log('public');")
    return root


def test_public_assets_are_deterministic_and_content_bound(tmp_path: Path) -> None:
    root = _ui(tmp_path)
    (root / "fonts").mkdir()
    (root / "fonts" / "local.woff2").write_bytes(b"local-font")
    (root / ".shell-bundle-manifest.json").write_text("{}")
    (root / "actions.py").write_text("# Existing compatibility Source, not UI")
    assets = compiled_ui_artifacts(root)
    assert [item["path"] for item in assets] == [
        "ui/fonts/local.woff2",
        "ui/shell-app.js",
        "ui/shell.html",
    ]
    assert all(item["kind"] == item["index_role"] == "asset" for item in assets)
    assert assets[0]["digest"] == "sha256:" + hashlib.sha256(b"local-font").hexdigest()
    (root / "shell-app.js").write_text("console.log('new');")
    assert compiled_ui_artifacts(root)[1]["digest"] != assets[1]["digest"]


@pytest.mark.parametrize("path", ["private.key", "backend.py", ".env", "bad\\file.js"])
def test_non_public_paths_are_rejected(tmp_path: Path, path: str) -> None:
    root = _ui(tmp_path)
    (root / path).write_text("not a UI asset")
    with pytest.raises(ValueError):
        compiled_ui_artifacts(root)


@pytest.mark.parametrize("kind", ["root", "ancestor", "leaf", "hidden"])
def test_ui_symlinks_are_rejected(tmp_path: Path, kind: str) -> None:
    root = _ui(tmp_path)
    if kind == "root":
        link = tmp_path / "linked-ui"
        link.symlink_to(root, target_is_directory=True)
        root = link
    elif kind == "ancestor":
        (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hidden":
        (root / ".shell-bundle-manifest.json").symlink_to(root / "shell.html")
    else:
        (root / "linked.js").symlink_to(root / "shell-app.js")
    with pytest.raises((OSError, ValueError)):
        compiled_ui_artifacts(root)


def test_hardlinked_and_oversize_assets_are_rejected(tmp_path: Path) -> None:
    root = _ui(tmp_path)
    os.link(root / "shell-app.js", root / "linked.js")
    with pytest.raises(ValueError, match="single-link"):
        compiled_ui_artifacts(root)
    (root / "linked.js").unlink()
    with (root / "large.wasm").open("wb") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="bounded"):
        compiled_ui_artifacts(root)


def test_symlink_above_ui_root_is_rejected(tmp_path: Path) -> None:
    root = _ui(tmp_path)
    link = tmp_path.parent / (tmp_path.name + "-linked-parent")
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        compiled_ui_artifacts(link / root.name)


def test_catalog_update_preserves_other_packs_and_authority(tmp_path: Path) -> None:
    root = _ui(tmp_path)
    original = {
        "packs": [
            {
                "pack_id": "defaultspack",
                "kind": "normal_sandbox",
                "version": "4.0.1",
                "capabilities": ["unchanged"],
                "provided_contracts": [{"exact": "same"}],
                "runtime_artifacts": [
                    {"path": "runtime/main.py", "kind": "executable", "digest": "existing"}
                ],
            },
            {"pack_id": "other", "version": "1.0.0", "runtime_artifacts": []},
        ]
    }
    before = copy.deepcopy(original)
    updated = catalog_with_compiled_ui(original, ui_root=root, version="4.0.2")
    assert original == before
    assert updated["packs"][1] == before["packs"][1]
    record = updated["packs"][0]
    assert record["version"] == "4.0.2"
    assert record["capabilities"] == before["packs"][0]["capabilities"]
    assert record["provided_contracts"] == before["packs"][0]["provided_contracts"]
    assert before["packs"][0]["runtime_artifacts"][0] in record["runtime_artifacts"]
    assert catalog_with_compiled_ui(updated, ui_root=root, version="4.0.2") == updated


def test_runtime_asset_collision_and_missing_shell_are_rejected(tmp_path: Path) -> None:
    root = _ui(tmp_path)
    catalog = {
        "packs": [
            {
                "pack_id": "defaultspack",
                "kind": "normal_sandbox",
                "runtime_artifacts": [{"path": "ui/shell.html", "kind": "executable"}],
            }
        ]
    }
    with pytest.raises(ValueError, match="collides"):
        catalog_with_compiled_ui(catalog, ui_root=root, version="4.0.2")
    (root / "shell.html").unlink()
    with pytest.raises(ValueError, match="shell.html"):
        compiled_ui_artifacts(root)


@pytest.mark.parametrize(
    "bound,value",
    [
        ("_MAX_TOTAL_BYTES", 1),
        ("_MAX_ASSET_COUNT", 1),
        ("_MAX_ENTRY_COUNT", 1),
        ("_MAX_DEPTH", 1),
        ("_MAX_PATH_BYTES", 10),
    ],
)
def test_aggregate_and_tree_bounds_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound: str,
    value: int,
) -> None:
    root = _ui(tmp_path)
    (root / "one" / "two").mkdir(parents=True)
    (root / "one" / "two" / "deep.js").write_text("public")
    monkeypatch.setattr(module, bound, value)
    with pytest.raises(ValueError):
        compiled_ui_artifacts(root)


@pytest.mark.parametrize("version", ["1", "01.0.0", "1.0.0-beta", "9" * 33 + ".0.0"])
def test_invalid_or_unbounded_release_versions_are_rejected(
    tmp_path: Path,
    version: str,
) -> None:
    with pytest.raises(ValueError, match="stable version"):
        catalog_with_compiled_ui({}, ui_root=tmp_path, version=version)


def test_regular_leaf_replaced_by_fifo_is_rejected_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _ui(tmp_path)
    original_open = os.open
    swapped = False

    def racing_open(
        path: str | Path,
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        nonlocal swapped
        if path == "shell-app.js" and not swapped:
            assert flags & os.O_NONBLOCK
            swapped = True
            os.unlink(path, dir_fd=kwargs["dir_fd"])
            os.mkfifo(root / path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", racing_open)
    with pytest.raises(ValueError, match="single-link"):
        compiled_ui_artifacts(root)
    assert swapped


@pytest.mark.parametrize("mutation", ["file", "inventory"])
def test_read_time_file_or_inventory_mutation_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root = _ui(tmp_path)
    original_read = os.read
    mutated = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal mutated
        block = original_read(descriptor, count)
        if block and not mutated:
            mutated = True
            if mutation == "file":
                with (root / "shell-app.js").open("ab") as stream:
                    stream.write(b"changed while reading")
            else:
                (root / "late.js").write_text("late inventory")
        return block

    monkeypatch.setattr(module.os, "read", racing_read)
    with pytest.raises(ValueError, match="changed while"):
        compiled_ui_artifacts(root)
    assert mutated
