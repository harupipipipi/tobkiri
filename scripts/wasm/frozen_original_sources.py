"""Resolve original-source evidence inputs that must stay frozen at their review.

The preserved Python 3.13 component evidence binds the *original* Defaults
Profile bytes as they were when the ordered bridge, Gateway caller, finalizer
and provider extractions were reviewed. The live Profile files are regenerated
whenever unrelated Packs gain edges, which changes only their whole-file hash.
Those frozen bytes are therefore kept verbatim under
``tests/fixtures/issue1520/frozen-original-sources`` and read through this
module; every other source is still read live from the repository.

Whole-file identity of the live Profile is intentionally not part of the
evidence. ``tests/test_frozen_original_sources.py`` instead proves that the
frozen bytes are exactly the pinned ones and that the live Profile did not
change any edge of a contract the frozen evidence covers.
"""

from __future__ import annotations

from pathlib import Path

SNAPSHOT_ROOT = (
    Path(__file__).resolve().parents[2]
    / "tests/fixtures/issue1520/frozen-original-sources"
)
SNAPSHOT_SUFFIX = ".frozen"
FROZEN_SOURCES = (
    "tobkiri_runtime/ecosystem/defaultspack/v4/defaults.profile.v5.json",
    "tobkiri_runtime/ecosystem/defaultspack/v4/defaults.profile.intent.v1.json",
)


def frozen_override(path: Path) -> Path:
    """Return the frozen snapshot for a frozen original source, else ``path``.

    Matching is by repository-relative suffix so isolated copies of the
    repository used by tests resolve to the same reviewed bytes.
    """
    text = Path(path).as_posix()
    for relative in FROZEN_SOURCES:
        if text == relative or text.endswith("/" + relative):
            return SNAPSHOT_ROOT / (relative + SNAPSHOT_SUFFIX)
    return Path(path)
