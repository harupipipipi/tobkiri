"""Compare the complete Rust retired-Pack migration with the pinned Python store."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from unittest.mock import patch

from original_authority_reference import ORIGINAL_HEAD, write_json
from original_setup_review_reference import check_original


def main() -> None:
    """Replay the original migration using the Rust test's exact input bytes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    fixture = args.fixture.resolve(strict=True)
    check_original(original)
    source = original / "tobkiri_runtime"
    sys.path.insert(0, str(source))
    from core_runtime import profile_content_projection, profile_definition_store_v4

    for module, filename in (
        (profile_definition_store_v4, "profile_definition_store_v4.py"),
        (profile_content_projection, "profile_content_projection.py"),
    ):
        if Path(module.__file__).resolve() != source / "core_runtime" / filename:
            raise ValueError(f"unexpected original source for {filename}")

    runtime = fixture / "runtime"
    projections = runtime / "profile_projections"
    local = projections / "local-agent"
    reference = projections / "reference-ui"
    (local / "a").mkdir(parents=True)
    reference.mkdir()
    (local / "a/é.md").write_bytes(b"nested")
    (local / "a.txt").write_bytes(b"top")
    (reference / "existing").write_bytes(b"ignored descriptor still verified")

    clock_reads = 0

    def clock() -> float:
        nonlocal clock_reads
        clock_reads += 1
        return 1700000400

    before = (fixture / "before.json").read_bytes()
    rust = (fixture / "rust.json").read_bytes()
    if len(before) > 65536 or len(rust) > 65536:
        raise ValueError("unexpected retired Pack migration test registry size")
    store_path = fixture / "python"
    store = profile_definition_store_v4.ProfileDefinitionStore(store_path, clock=clock)
    store.path.write_bytes(before)
    store.path.chmod(0o600)
    with (
        patch.object(profile_content_projection, "RUNTIME_ROOT", runtime),
        patch.object(profile_content_projection, "PROJECTION_ROOT", projections),
    ):
        count = store.migrate_retired_pack_projections()
        if count != 2 or clock_reads != 1 or store.path.read_bytes() != rust:
            raise ValueError("original retired Pack migration differs from Rust")
        reopened = profile_definition_store_v4.ProfileDefinitionStore(
            store_path, clock=clock
        )
        if (
            reopened.migrate_retired_pack_projections() != 0
            or clock_reads != 1
            or reopened.path.read_bytes() != rust
        ):
            raise ValueError("original retired Pack migration replay differs")

    write_json(
        fixture / "comparison.json",
        {
            "original_head": ORIGINAL_HEAD,
            "migrated_profiles": count,
            "exact_registry_bytes": len(rust),
            "clock_reads": clock_reads,
            "replay_writes": False,
        },
    )


if __name__ == "__main__":
    main()
