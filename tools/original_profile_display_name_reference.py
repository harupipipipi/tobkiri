"""Compare the complete Rust registry write with the pinned Python operation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from original_authority_reference import ORIGINAL_HEAD, write_json
from original_setup_review_reference import check_original


def main() -> None:
    """Replay the original store against the exact pre-repair bytes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    fixture = args.fixture.resolve(strict=True)
    check_original(original)
    runtime = original / "tobkiri_runtime"
    sys.path.insert(0, str(runtime))
    from core_runtime import profile_definition_store_v4 as definitions

    if Path(definitions.__file__).resolve() != (
        runtime / "core_runtime/profile_definition_store_v4.py"
    ):
        raise ValueError("unexpected original Profile definition source")

    clock_reads = 0

    def clock() -> float:
        nonlocal clock_reads
        clock_reads += 1
        return 1700000300

    store = definitions.ProfileDefinitionStore(fixture / "python", clock=clock)
    before = (fixture / "before.json").read_bytes()
    rust = (fixture / "rust.json").read_bytes()
    if len(before) > 65536 or len(rust) > 65536:
        raise ValueError("unexpected display-name test registry size")
    store.path.write_bytes(before)
    store.path.chmod(0o600)
    count = store.repair_legacy_display_names()
    if count != 5 or clock_reads != 1 or store.path.read_bytes() != rust:
        raise ValueError("original localized-name repair differs from Rust")
    reopened = definitions.ProfileDefinitionStore(fixture / "python", clock=clock)
    if (
        reopened.repair_legacy_display_names() != 0
        or clock_reads != 1
        or reopened.path.read_bytes() != rust
    ):
        raise ValueError("original localized-name repair replay differs")
    write_json(
        fixture / "comparison.json",
        {
            "original_head": ORIGINAL_HEAD,
            "repaired": count,
            "exact_registry_bytes": len(rust),
            "clock_reads": clock_reads,
            "replay_writes": False,
        },
    )


if __name__ == "__main__":
    main()
