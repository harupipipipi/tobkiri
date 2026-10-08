"""Signed revision admission retains the baseline and rejects stale updates."""

from pathlib import Path
from typing import Any

import pytest

from core_runtime.external_pack_catalog_v4 import (
    ExternalPackCatalogDenied,
    admit_signed_external_pack,
    admit_signed_pack_version,
    control_catalog_revision,
    load_external_pack_catalog,
    resolve_admitted_pack_root,
)
from tests.test_external_pack_catalog_v4 import PACK_ID, _signed_external_pack


def _initial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Path]:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, trust = _signed_external_pack(tmp_path)
    first = admit_signed_external_pack(source, trust_store_path=trust)
    parent = tmp_path / "new-release"
    parent.mkdir()
    return dict(first), parent


def test_signed_update_retains_old_cas_and_requires_explicit_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, parent = _initial(tmp_path, monkeypatch)
    old_root = resolve_admitted_pack_root(PACK_ID)
    old_bytes = (old_root / "runtime/echo.py").read_bytes()
    source, trust = _signed_external_pack(
        parent,
        version="1.1.0",
        runtime_suffix="\n# signed revision two\n",
    )
    revision = control_catalog_revision()
    second = admit_signed_pack_version(
        source,
        trust_store_path=trust,
        expected_predecessor_digest=first["artifact_digest"],
        expected_catalog_revision=revision,
    )
    assert second["artifact_digest"] != first["artifact_digest"]
    assert control_catalog_revision() != revision
    assert load_external_pack_catalog().entries[PACK_ID] == first
    assert resolve_admitted_pack_root(PACK_ID) == old_root
    assert (old_root / "runtime/echo.py").read_bytes() == old_bytes
    selected = load_external_pack_catalog({PACK_ID: second["artifact_digest"]})
    assert selected.entries[PACK_ID] == second
    assert len(selected.versions[PACK_ID]) == 2
    new_root = resolve_admitted_pack_root(
        PACK_ID,
        artifact_digest=second["artifact_digest"],
    )
    assert new_root != old_root
    assert (new_root / "runtime/echo.py").read_bytes() == (source / "runtime/echo.py").read_bytes()
    assert (
        resolve_admitted_pack_root(
            PACK_ID,
            artifact_digest=first["artifact_digest"],
        )
        == old_root
    )


@pytest.mark.parametrize("change", ["catalog", "predecessor", "publisher", "version"])
def test_update_conflicts_leave_old_catalog_and_artifact_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    first, parent = _initial(tmp_path, monkeypatch)
    old = load_external_pack_catalog()
    source, trust = _signed_external_pack(
        parent,
        version="1.0.0" if change == "version" else "1.1.0",
        publisher_id="publisher.attacker" if change == "publisher" else "publisher.conformance",
        runtime_suffix="\n# changed runtime\n",
    )
    with pytest.raises(ExternalPackCatalogDenied):
        admit_signed_pack_version(
            source,
            trust_store_path=trust,
            expected_predecessor_digest=(
                "sha256:" + "f" * 64 if change == "predecessor" else first["artifact_digest"]
            ),
            expected_catalog_revision=(
                "sha256:" + "e" * 64 if change == "catalog" else control_catalog_revision()
            ),
        )
    assert load_external_pack_catalog() == old


def test_unknown_selected_digest_never_falls_back_to_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial(tmp_path, monkeypatch)
    with pytest.raises(ExternalPackCatalogDenied, match="not admitted"):
        resolve_admitted_pack_root(PACK_ID, artifact_digest="sha256:" + "d" * 64)
