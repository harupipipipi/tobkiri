"""The isolated Catalog Pack uses an exact, reproducible public-contract copy."""

import importlib.util
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1] / "tobkiri_runtime"
_SPEC = importlib.util.spec_from_file_location(
    "catalog_runtime_generator", _ROOT / "scripts/generate_model_catalog_runtime.py",
)
generator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(generator)


def test_sealed_filter_contract_matches_canonical_public_source() -> None:
    assert generator.generate(check=True) == _ROOT / generator.DESTINATION


def test_generator_writes_exact_source_bytes(tmp_path: Path) -> None:
    source = tmp_path / generator.SOURCE
    destination = tmp_path / generator.DESTINATION
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_bytes(b'"""Canonical public contract fixture."""\n')
    assert generator.generate(check=False, root=tmp_path) == destination
    assert destination.read_bytes() == source.read_bytes()
    generator.generate(check=True, root=tmp_path)


@pytest.mark.parametrize("damage", ["missing", "divergent"])
def test_generator_check_rejects_drift_without_repairing_it(
    tmp_path: Path, damage: str,
) -> None:
    source = tmp_path / generator.SOURCE
    destination = tmp_path / generator.DESTINATION
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_bytes(b'"""Canonical public contract fixture."""\n')
    if damage == "divergent":
        destination.write_bytes(b"changed = True\n")
    with pytest.raises(ValueError, match="sealed model filter contract is stale"):
        generator.generate(check=True, root=tmp_path)
    if damage == "missing":
        assert not destination.exists()
    else:
        assert destination.read_bytes() == b"changed = True\n"
