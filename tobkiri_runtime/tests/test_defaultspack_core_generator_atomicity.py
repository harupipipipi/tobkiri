from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

import pytest


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
    expected = {
        path.relative_to(generator.BUNDLE).as_posix(): raw
        for path, raw in rendered.items()
    }

    assert _snapshot(generator.BUNDLE) == expected


def test_core_generator_transaction_rejects_destination_symlink(
    tmp_path: Path,
) -> None:
    generator = _load_generator()
    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE, copied)
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
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
