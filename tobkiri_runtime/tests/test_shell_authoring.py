"""Actual platform artifact checks for unbound Shell authoring descriptors."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.platform_artifact import verify_platform_artifact
from tobkiri_protocol.shell_authoring import describe_shell_variant


def _source(tmp_path: Path) -> tuple[dict, Path]:
    source = json.loads((Path(__file__).resolve().parents[1]
        / "ecosystem/defaultspack/v4/shell.cli.default.shell.v1.json").read_bytes())
    source["launch"]["build_targets"] = [{
        "artifact_id": "example.cli.linux-x86_64", "platform": "linux",
        "architecture": "x86_64", "artifact_ref": "bin/shell",
        "entrypoint": "bin/shell", "bundle_identity": "example.cli",
    }]
    _revision(source)
    (tmp_path / "bin").mkdir()
    # Architecture-only fixture, never executable or a real Shell launch.
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = (62).to_bytes(2, "little")
    (tmp_path / "bin/shell").write_bytes(header)
    return source, tmp_path


def _revision(source: dict) -> None:
    source["definition_revision"] = canonical_digest({
        key: value for key, value in source.items() if key != "definition_revision"
    })


def test_descriptor_uses_exact_declared_bytes_without_mutating_source(tmp_path):
    source, root = _source(tmp_path)
    before = deepcopy(source)
    variant = describe_shell_variant(source, root, platform="linux", architecture="x86_64")
    assert source == before and source["availability"] == "build_required"
    assert source["launch"]["variants"] == []
    assert variant["relative_path"] == variant["entrypoint"] == "bin/shell"
    verify_platform_artifact(root, variant)
    (root / "bin/shell").write_bytes(b"changed")
    with pytest.raises(ProtocolError, match="digest"):
        verify_platform_artifact(root, variant)


@pytest.mark.parametrize("change", ["outside", "duplicate", "stale", "architecture"])
def test_invalid_source_or_platform_artifact_is_denied(tmp_path, change):
    source, root = _source(tmp_path)
    expected = "revision" if change == "stale" else (
        "architecture" if change == "architecture" else (
            "duplicated" if change == "duplicate" else "outside"
        )
    )
    if change == "outside":
        (root / "outside").write_bytes(b"outside")
        source["launch"]["build_targets"][0]["entrypoint"] = "outside"
    elif change == "duplicate":
        extra = deepcopy(source["launch"]["build_targets"][0])
        extra["artifact_id"] = "example.other"
        source["launch"]["build_targets"].append(extra)
    elif change == "architecture":
        (root / "bin/shell").write_bytes(b"not a binary")
    else:
        source["definition_revision"] = "sha256:" + "1" * 64
    if change != "stale":
        _revision(source)
    with pytest.raises(ProtocolError, match=expected):
        describe_shell_variant(source, root, platform="linux", architecture="x86_64")
