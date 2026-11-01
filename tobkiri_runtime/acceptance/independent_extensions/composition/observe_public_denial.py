"""Observe the documented Host API denial with no publisher policy.

This deliberately stops before signature/executable checks and never proves
valid admission, activation, selection or execution. No live state is used.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile

from core_runtime.external_pack_catalog_v4 import admit_signed_external_pack


def snapshot(root: Path) -> dict[str, str]:
    """Hash the owned scaffold files without interpreting their contents."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def observe() -> dict[str, object]:
    """Record denial and unchanged source for both independent scaffold roots."""
    root = Path(__file__).resolve().parents[1]
    rows = []
    for leaf in (
        "cli_presentation/acceptance.cli.presentation",
        "temporal_context/acceptance.temporal.context",
    ):
        source = root / leaf
        before = snapshot(source)
        with tempfile.TemporaryDirectory(prefix="tobkiri-public-no-policy-") as folder:
            policy = Path(folder) / "host-policy" / "publisher-trust.json"
            try:
                admit_signed_external_pack(source, trust_store_path=policy)
            except Exception as error:
                rows.append({
                    "source_pack": leaf,
                    "source_file_hashes": before,
                    "outcome": "denied",
                    "exception_type": type(error).__name__,
                    "reason": str(error)[:600],
                    "source_unchanged": before == snapshot(source),
                })
            else:
                raise AssertionError("Absent Host publisher policy admitted source")
    return {
        "schema": "tobkiri.independent-extension.public-denial-observation.v1",
        "documented_public_api": "core_runtime.external_pack_catalog_v4.admit_signed_external_pack",
        "rows": rows,
        "policy_supplied": False,
        "native_consent_or_activation": False,
        "production_execution": False,
        "limits": "Actual denial is the absent/unsafe Host publisher-policy transaction, before signature/executable validation. This does not establish signature checking, executable capture or selection/composition acceptance.",
    }


if __name__ == "__main__":
    print(json.dumps(observe(), indent=2))
