"""Generate the pinned original's legacy Activation records for native CI."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from original_authority_reference import write_json
from original_setup_review_reference import check_original


def generate(runtime: Path, output: Path) -> None:
    sys.path.insert(0, str(runtime))
    from core_runtime.authority.v4 import AuthorityStore
    from ecosystem.defaultspack.domain.runtime_v4 import ActivationStore
    from tests import test_profile_architecture_review_c as fixture

    if Path(fixture.__file__).resolve() != runtime / "tests/test_profile_architecture_review_c.py":
        raise ValueError("another legacy activation fixture was imported")

    with TemporaryDirectory(prefix="original-legacy-activation-", dir=output.parent) as directory:
        root = Path(directory)
        catalog = fixture._legacy_packaged_catalog(root / "catalog")
        resolved = fixture._resolve(catalog)
        records = fixture._compatible_legacy_fixture(resolved)
        workspace = root / "workspace"
        workspace.mkdir()
        state = root / "state"
        authority = AuthorityStore(root / "authority.sqlite3")
        fixture._write_frozen_activation(state, workspace, authority, records)
        store = ActivationStore(
            state, workspace, profile_id="defaults", authority=authority, catalog=catalog
        )
        migrated = store.load_active_snapshot()
        if (
            migrated.resolved != resolved
            or store.load_active_snapshot().activation != migrated.activation
        ):
            raise AssertionError("original legacy activation did not migrate to this candidate")
        records["legacy_successor"] = {
            "profile": migrated.resolved.profile,
            "lock": migrated.resolved.lock,
            "plan": migrated.resolved.plan,
        }
        records["migrated_activation"] = migrated.activation
        write_json(output, records, compact=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    check_original(original)
    generate(original / "tobkiri_runtime", args.output.resolve())


if __name__ == "__main__":
    main()
