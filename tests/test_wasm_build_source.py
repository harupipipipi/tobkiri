"""Build inputs are explicit and verified before compiler initialization."""

import hashlib
from pathlib import Path

import pytest

from scripts.wasm.build_shell_policy import build, capture_source


def test_capture_pins_bytes_without_reopening_source(tmp_path: Path) -> None:
    """A later file replacement cannot alter the captured compiler input."""
    source = tmp_path / "policy.py"
    original = b"def classify(command): return command\n"
    source.write_bytes(original)
    captured = capture_source(source, hashlib.sha256(original).hexdigest())
    source.write_bytes(b"changed")
    assert captured == original


@pytest.mark.parametrize("pin", ["", "a" * 63, "A" * 64, "g" * 64, "0" * 64])
def test_bad_pin_fails_before_build_side_effects(tmp_path: Path, pin: str) -> None:
    """Neither optional compiler imports nor output creation precede verification."""
    source = tmp_path / "policy.py"
    source.write_bytes(b"original")
    output = tmp_path / "new" / "policy.wasm"
    with pytest.raises(ValueError, match="SHA-256"):
        build(output, source=source, source_sha256=pin)
    assert not output.parent.exists()


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink"])
def test_non_regular_source_is_rejected(tmp_path: Path, kind: str) -> None:
    """An explicit digest does not permit a non-regular selected source."""
    source = tmp_path / "policy.py"
    if kind == "directory":
        source.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target.py"
        target.write_bytes(b"original")
        try:
            source.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation unavailable on this host")
    with pytest.raises(ValueError, match="regular"):
        capture_source(source, hashlib.sha256(b"original").hexdigest())
