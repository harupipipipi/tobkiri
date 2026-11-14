"""Compare the Rust first-install registry mutation with the pinned original."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from original_authority_reference import ORIGINAL_HEAD, write_json
from original_setup_review_reference import check_original


def main() -> None:
    """Repeat the original creation and lookup with the exact native test data."""
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
        return 23

    before, template, native, replay = [
        (fixture / name).read_bytes()
        for name in ("before.json", "template.json", "actual.json", "replay.json")
    ]
    if max(map(len, (before, template, native, replay))) > 65536:
        raise ValueError("unexpected bootstrap template fixture size")
    template = json.loads(template)
    store = definitions.ProfileDefinitionStore(fixture / "python", clock=clock)
    store.path.write_bytes(before)
    store.path.chmod(0o600)
    if store.bootstrap_state()["state"] != "empty":
        raise ValueError("bootstrap template fixture registry is not empty")
    created = store.bootstrap_defaults(template)
    if clock_reads != 1 or store.path.read_bytes() != native:
        raise ValueError("original bootstrap template registration differs from Rust")
    reopened = definitions.ProfileDefinitionStore(fixture / "python", clock=clock)
    existing = reopened.bootstrap_defaults(template)
    if (
        existing.profile_revision != created.profile_revision
        or clock_reads != 1
        or reopened.path.read_bytes() != replay
        or replay != native
    ):
        raise ValueError("original bootstrap template lookup replay differs")

    write_json(
        fixture / "comparison.json",
        {
            "original_head": ORIGINAL_HEAD,
            "exact_registry_bytes": len(native),
            "clock_reads": clock_reads,
            "replay_writes": False,
        },
    )


if __name__ == "__main__":
    main()
